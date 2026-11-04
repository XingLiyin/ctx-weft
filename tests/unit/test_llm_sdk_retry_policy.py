"""Keep HTTP retry decisions consistent with the installed OpenAI SDK."""

import httpx
import pytest
from openai import OpenAI

from ctx_weft.providers.llm.adapters._errors import http_error


@pytest.mark.parametrize("retry_header", [None, "true", "false", "True", "false "])
@pytest.mark.parametrize("error_code", [
    "insufficient_quota", "authentication_error", "api_error", "timeout_error",
])
def test_http_retry_decisions_match_sdk_for_all_error_statuses(retry_header, error_code):
    message = "upstream reason\nhttps://gateway.example/request?token=test"
    headers = {"retry-after": "2"}
    if retry_header is not None:
        headers["x-should-retry"] = retry_header
    with OpenAI(api_key="test", http_client=httpx.Client(trust_env=False)) as sdk:
        for status in range(400, 600):
            response = httpx.Response(
                status, headers=headers,
                json={"error": {"code": error_code, "message": message}},
                request=httpx.Request("POST", "https://gateway.example/llm"),
            )
            error = http_error("OpenAI", response)

            assert error.retriable is sdk._should_retry(response), (status, retry_header)
            assert error.outage is error.retriable
            assert error.status_code == status
            assert error.retry_after_sec == 2
            assert str(error) == message
