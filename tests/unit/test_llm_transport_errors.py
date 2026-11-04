"""Classify chained transport failures and preserve their messages."""

import logging
import ssl

import httpcore
import httpx
import pytest

from ctx_weft.providers.llm.adapters._errors import transport_error

_SECRET = "Bearer private-key https://private.example/path?token=secret private-prompt"
_LOGGER = "ctx_weft.providers.llm.adapters._errors"


@pytest.mark.parametrize("fault", [httpx.ReadError, httpx.ReadTimeout])
@pytest.mark.parametrize("tls_fault", [
    ssl.SSLWantReadError, ssl.SSLWantWriteError, ssl.SSLEOFError,
    ssl.SSLZeroReturnError, ssl.SSLError,
])
def test_interrupted_reads_remain_retriable_with_tls_context(fault, tls_fault):
    inner = httpcore.ReadError(_SECRET)
    inner.__context__ = tls_fault(_SECRET)
    inner.__suppress_context__ = True
    exc = fault(_SECRET)
    exc.__cause__ = inner

    error = transport_error("OpenAI", exc, produced=True)

    assert error.retriable is True
    assert error.outage is True
    assert str(error) == _SECRET


@pytest.mark.parametrize("link", ["__cause__", "__context__"])
def test_certificate_connection_failure_remains_retriable_like_sdk(link):
    exc = httpx.ConnectError(_SECRET)
    setattr(exc, link, ssl.SSLCertVerificationError(_SECRET))

    error = transport_error("OpenAI", exc, produced=False)

    assert error.retriable is True
    assert error.outage is True
    assert str(error) == _SECRET


@pytest.mark.parametrize(("fault", "retriable"), [
    (httpx.InvalidURL, False), (httpx.UnsupportedProtocol, True),
    (httpx.LocalProtocolError, True), (httpx.DecodingError, False),
])
def test_unwrapped_httpx_failure_retryability(fault, retriable):
    error = transport_error("OpenAI", fault(_SECRET), produced=False)

    assert error.retriable is retriable
    assert error.outage is retriable
    assert str(error) == _SECRET


@pytest.mark.parametrize("fault", [httpx.ReadError, httpx.ReadTimeout])
def test_logs_include_exception_types_and_original_message(caplog, fault):
    exc = fault(_SECRET, request=httpx.Request(
        "POST", "https://private.example/path?token=secret",
        headers={"Authorization": "Bearer private-key"}, content="private-prompt",
    ))
    exc.__cause__ = httpcore.ReadError(_SECRET)
    exc.__cause__.__context__ = ssl.SSLEOFError(_SECRET)
    exc.__cause__.__suppress_context__ = True
    exc.__context__ = ssl.SSLWantReadError(_SECRET)
    exc.__context__.__context__ = exc

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        transport_error("OpenAI", exc, produced=True)

    records = [record for record in caplog.records if record.name == _LOGGER]
    assert len(records) == 1
    message = records[0].getMessage()
    assert "category=" not in message
    assert f"httpx.{fault.__name__}" in message
    assert "httpcore.ReadError" in message
    assert "ssl.SSLEOFError" not in message
    assert "ssl.SSLWantReadError" not in message
    assert "retriable=True" in message
    assert records[0].exc_info is None
    assert records[0].stack_info is None
    assert _SECRET in caplog.text


def test_certificate_connection_log_preserves_message_and_retry_decision(caplog):
    exc = httpx.ConnectError(_SECRET)
    exc.__cause__ = ssl.SSLCertVerificationError(_SECRET)

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        transport_error("Anthropic", exc, produced=False)

    assert "ssl.SSLCertVerificationError" in caplog.text
    assert "retriable=True" in caplog.text
    assert _SECRET in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("explicit_cause", [False, True])
def test_unrelated_certificate_context_does_not_disable_retry(explicit_cause):
    exc = httpx.ReadError("connection reset")
    exc.__context__ = ssl.SSLCertVerificationError("previous failure")
    exc.__suppress_context__ = True
    if explicit_cause:
        exc.__cause__ = ConnectionResetError("connection reset")

    error = transport_error("OpenAI", exc, produced=True)

    assert error.retriable and error.outage
    assert str(error) == "connection reset"


def test_cyclic_exception_chain_does_not_prevent_retry():
    exc = httpx.ReadError("connection reset")
    exc.__cause__ = httpcore.ReadError("connection reset")
    exc.__cause__.__cause__ = exc
    assert transport_error("OpenAI", exc, produced=True).retriable
