"""Single-attempt adapter errors; retry policy belongs to the loop gateway."""

from __future__ import annotations

import math
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx

from ctx_weft.protocols import LLMCallError

_RETRIABLE_CODES = frozenset({429, 500, 502, 503, 504})


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            delay = (when - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError, OverflowError):
            return None
    return delay if math.isfinite(delay) and delay >= 0 else None


def http_error(provider: str, response: httpx.Response) -> LLMCallError:
    """Do not copy upstream bodies, URLs or credentials into events/errors."""
    status = response.status_code
    advice = {
        400: "Check the model and request configuration.",
        401: "Check the API credentials.",
        403: "Check account permissions and access policy.",
        404: "Check the endpoint and model name.",
    }.get(status, "The model service rejected the request.")
    retriable = status in _RETRIABLE_CODES
    return LLMCallError(
        f"{provider} API error {status}. {advice}",
        status_code=status,
        retriable=retriable,
        outage=retriable,
        retry_after_sec=_retry_after(response.headers.get("retry-after")),
    )


def transport_error(provider: str, exc: Exception, *, produced: bool) -> LLMCallError:
    # httpx wraps certificate errors in ConnectError. Inspect types, never strings
    # (which may include credentials, proxy URLs or upstream content).
    pending: list[BaseException] = [exc]
    seen: set[int] = set()
    tls_error = False
    while pending:
        cause = pending.pop()
        if id(cause) in seen:
            continue
        seen.add(id(cause))
        tls_error |= isinstance(cause, ssl.SSLError)
        pending.extend(e for e in (cause.__cause__, cause.__context__) if e is not None)
    retriable = not tls_error and isinstance(
        exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, httpx.ProxyError)
    )
    phase = "stream interrupted" if produced else "connection failed"
    advice = " Check the TLS certificate configuration." if tls_error else ""
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol, httpx.LocalProtocolError)):
        advice = " Check the endpoint and HTTP configuration."
    return LLMCallError(
        f"{provider} {phase} ({type(exc).__name__}).{advice}",
        retriable=retriable,
        outage=retriable,
    )
