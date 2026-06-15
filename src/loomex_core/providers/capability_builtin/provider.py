"""BuiltinToolsCapabilityProvider：进程内"其他可选"工具集（非文件系统）。

文件系统操作（bash_exec / read_file / write_file / glob）已拆到
loomex_core.providers.capability_filesystem（带 per-session workspace 管理）。
本 provider 保留与工作目录无关的通用工具，目前为 http_request。

工具用 @tool 装饰器声明，函数体即实现，input_schema 由 extract_schema() 自动提取。
"""

from __future__ import annotations

import dataclasses
import ipaddress
import logging
import socket
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlparse

import httpx

from loomex_core.protocols.capability import (
    Capability,
    CapabilityEvent,
    CapabilityProviderInfo,
    ToolCapabilityProvider,
)
from loomex_core.protocols.context import ProviderContext
from loomex_core.providers._tooldecl import make_tool_registry

logger = logging.getLogger(__name__)

PROVIDER_NAME = "builtin"

tool, _BUILTIN_TOOLS, _BUILTIN_IMPLS = make_tool_registry(PROVIDER_NAME)

# ── 共享常量 ──────────────────────────────────────────────────────────────────

_HTTP_STRIP_HEADERS = frozenset([
    "authorization", "cookie", "x-api-key", "x-auth-token",
    "proxy-authorization", "www-authenticate",
])
_HTTP_TIMEOUT_SEC_DEFAULT = 30
_HTTP_MAX_RESPONSE_BYTES_DEFAULT = 1_000_000


def _is_ssrf_target(hostname: str) -> bool:
    try:
        addr = socket.gethostbyname(hostname)
        ip = ipaddress.ip_address(addr)
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
    except Exception:
        return False


# ── 工具实现 ──────────────────────────────────────────────────────────────────


@tool(purposes=["act"], side_effects=True)
async def http_request(
    url: Annotated[str, "URL to send the request to"],
    method: Annotated[str, "HTTP method: GET | POST | PUT | DELETE | PATCH"] = "GET",
    headers: Annotated[dict, "Request headers (key-value pairs)"] = None,
    body: Annotated[dict, "Request body sent as JSON"] = None,
    params: Annotated[dict, "Query string parameters"] = None,
    *,
    ctx: ProviderContext | None = None,
) -> AsyncIterator[CapabilityEvent]:
    """Make an HTTP request and return the response body."""
    if not url:
        yield CapabilityEvent(kind="error", payload={"code": "MISSING_URL", "message": "url is required"})
        return

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        yield CapabilityEvent(
            kind="error",
            payload={"code": "INVALID_SCHEME", "message": "Only http/https are allowed"},
        )
        return

    if _is_ssrf_target(parsed.hostname or ""):
        yield CapabilityEvent(
            kind="error",
            payload={"code": "SSRF_BLOCKED", "message": "Requests to internal addresses are blocked"},
        )
        return

    method = (method or "GET").upper()
    safe_headers = {
        k: v for k, v in (headers or {}).items()
        if k.lower() not in _HTTP_STRIP_HEADERS
    }

    _timeout = (ctx.extra.get("http_timeout_sec") if ctx else None) or _HTTP_TIMEOUT_SEC_DEFAULT
    _max_bytes = (ctx.extra.get("http_max_response_bytes") if ctx else None) or _HTTP_MAX_RESPONSE_BYTES_DEFAULT

    yield CapabilityEvent(kind="progress", payload={"status": "sending", "method": method, "url": url})

    try:
        async with httpx.AsyncClient(timeout=_timeout, follow_redirects=True) as client:
            if isinstance(body, dict):
                response = await client.request(
                    method, url, headers=safe_headers, json=body, params=params or {},
                )
            elif body is not None:
                response = await client.request(
                    method, url, headers=safe_headers, content=str(body).encode(), params=params or {},
                )
            else:
                response = await client.request(
                    method, url, headers=safe_headers, params=params or {},
                )

        is_error = response.status_code >= 400
        yield CapabilityEvent(
            kind="result",
            payload={
                "content": response.text[:_max_bytes],
                "metadata": {
                    "status_code": response.status_code,
                    "content_type": response.headers.get("content-type", ""),
                    "is_error": is_error,
                },
            },
        )
    except httpx.TimeoutException:
        yield CapabilityEvent(
            kind="error",
            payload={"code": "TIMEOUT", "message": f"Request timed out after {_timeout}s"},
        )
    except Exception as e:
        logger.exception("http_request failed: %s", url)
        yield CapabilityEvent(kind="error", payload={"code": "REQUEST_ERROR", "message": str(e)})


# ── Config ────────────────────────────────────────────────────────────────────


@dataclass
class BuiltinToolsConfig:
    """Provider 运行时配置。权限控制由 auth 层负责，不在此处理。"""
    allowed_dirs: list[Path] = field(default_factory=list)
    http_timeout_sec: int = 30
    http_max_response_bytes: int = 1_000_000


# ── Provider ──────────────────────────────────────────────────────────────────


class BuiltinToolsCapabilityProvider(ToolCapabilityProvider):
    """Provides non-filesystem builtin tools (currently http_request)."""

    name = PROVIDER_NAME

    def __init__(self, config: BuiltinToolsConfig | None = None) -> None:
        self._cfg = config or BuiltinToolsConfig()
        self._invokers = self._build_invokers()

    def _build_invokers(self) -> dict[str, Callable]:
        return {
            name: (lambda f: lambda args, ctx: f(**args, ctx=ctx))(fn)
            for name, fn in _BUILTIN_IMPLS.items()
        }

    async def list(self, ctx: ProviderContext) -> list[Capability]:
        return list(_BUILTIN_TOOLS.values())

    def invoke(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        extra = dict(ctx.extra)
        extra["http_timeout_sec"] = self._cfg.http_timeout_sec
        extra["http_max_response_bytes"] = self._cfg.http_max_response_bytes
        ctx = dataclasses.replace(ctx, extra=extra)
        return self._dispatch(capability_id, arguments, ctx)

    async def _dispatch(
        self,
        capability_id: str,
        arguments: dict[str, Any],
        ctx: ProviderContext,
    ) -> AsyncIterator[CapabilityEvent]:
        name = capability_id.split(":")[-1]
        invoker = self._invokers.get(name)
        if invoker is None:
            yield CapabilityEvent(
                kind="error",
                payload={"code": "UNKNOWN_CAPABILITY", "message": f"Unknown: {capability_id}"},
            )
            return
        async for ev in invoker(arguments, ctx):
            yield ev

    async def cancel(self, invocation_id: str, ctx: ProviderContext) -> None:
        pass

    async def describe(self, ctx: ProviderContext) -> CapabilityProviderInfo:
        return CapabilityProviderInfo(
            name=self.name,
            capability_count=len(_BUILTIN_TOOLS),
            supports_streaming=True,
            supports_cancel=False,
            description=self.description,
        )
