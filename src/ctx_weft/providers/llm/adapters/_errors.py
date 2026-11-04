"""Single-attempt adapter errors; retry policy belongs to the loop gateway."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ctx_weft.protocols import LLMCallError

logger = logging.getLogger(__name__)

_RETRIABLE_SSE_ERRORS = frozenset({
    "overloaded_error", "rate_limit_error", "rate_limit_exceeded",
    "api_error", "server_error", "timeout_error",
})


def _should_retry(response: httpx.Response) -> bool:
    should_retry = response.headers.get("x-should-retry")
    if should_retry in {"true", "false"}:
        return should_retry == "true"
    return response.status_code in {408, 409, 429} or response.status_code >= 500


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            delay = (when - datetime.now(UTC)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def provider_error(
    provider: str, body: Any, *, status_code: int = 0,
    retry_after_sec: float | None = None, operation: str = "stream",
    retriable: bool = False,
) -> LLMCallError:
    error = body.get("error", body) if isinstance(body, dict) else body
    fields = error if isinstance(error, dict) else {}
    detail = fields.get("message")
    if detail is None and isinstance(body, dict):
        detail = body.get("message")
    if detail is None:
        detail = error
    message = str(detail) if detail is not None and detail != "" else (
        f"HTTP {status_code}" if status_code else f"{provider} {operation} error"
    )
    return LLMCallError(
        message,
        status_code=status_code,
        retriable=retriable,
        outage=retriable,
        retry_after_sec=retry_after_sec,
    )


def http_error(provider: str, response: httpx.Response) -> LLMCallError:
    try:
        body = response.json()
    except ValueError:
        body = response.text
    except httpx.ResponseNotRead:
        body = None
    return provider_error(
        provider, body, status_code=response.status_code,
        retry_after_sec=_retry_after(response.headers.get("retry-after")),
        retriable=_should_retry(response),
    )


def sse_error(provider: str, body: Any) -> LLMCallError:
    error = body.get("error", body) if isinstance(body, dict) else body
    fields = error if isinstance(error, dict) else {}
    retriable = any(
        isinstance(value, str) and value in _RETRIABLE_SSE_ERRORS
        for value in (fields.get("code"), fields.get("type"))
    )
    return provider_error(provider, body, retriable=retriable)


async def stream_http_error(provider: str, response: httpx.Response) -> LLMCallError:
    try:
        await response.aread()
    except httpx.TransportError as exc:
        logger.warning(
            "LLM error details unavailable provider=%s status=%s exception_type=%s detail=%s",
            provider, response.status_code, type(exc).__name__, exc,
        )
        return provider_error(
            provider, str(exc), status_code=response.status_code,
            retry_after_sec=_retry_after(response.headers.get("retry-after")),
            retriable=_should_retry(response),
        )
    return http_error(provider, response)


def transport_error(
    provider: str, exc: Exception, *, produced: bool, timeout: httpx.Timeout | None = None,
    retriable: bool | None = None,
) -> LLMCallError:
    cause: BaseException | None = exc
    seen: set[int] = set()
    exception_types: list[str] = []
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        exception_types.append(f"{type(cause).__module__}.{type(cause).__name__}")
        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
    if retriable is None:
        retriable = isinstance(exc, httpx.TransportError)
    timeout_details = ""
    if timeout is not None:
        timeout_details = (
            f" connect_timeout_sec={timeout.connect} read_timeout_sec={timeout.read}"
            f" write_timeout_sec={timeout.write} pool_timeout_sec={timeout.pool}"
        )
    detail = str(exc) or type(exc).__name__
    logger.warning(
        "LLM request failed provider=%s exception_types=%s retriable=%s stream_started=%s detail=%s%s",
        provider, " -> ".join(exception_types), retriable, produced, detail, timeout_details,
    )
    return LLMCallError(
        detail,
        retriable=retriable,
        outage=retriable,
    )
