"""Model discovery shares HTTP and transport errors with inference."""

import httpx

from ctx_weft.protocols import LLMCallError
from ctx_weft.providers.llm.adapters._errors import http_error, provider_error, transport_error


def parse_model_ids(provider: str, response: httpx.Response) -> list[str]:
    try:
        body = response.json()
    except ValueError:
        raise LLMCallError(
            f"{provider} model discovery returned invalid JSON", retriable=False,
        ) from None
    if isinstance(body, dict) and body.get("error") is not None:
        raise provider_error(provider, body, operation="model discovery") from None
    if not isinstance(body, dict) or not isinstance(body.get("data"), list):
        raise LLMCallError(
            f"{provider} model discovery returned an invalid model list", retriable=False,
        )
    return [item["id"] for item in body["data"]
            if isinstance(item, dict) and isinstance(item.get("id"), str)]


async def list_model_ids(
    provider: str, client: httpx.AsyncClient, url: str,
    headers: dict[str, str], timeout: httpx.Timeout,
) -> list[str]:
    try:
        response = await client.get(url, headers=headers, timeout=timeout)
        if not response.is_success:
            raise http_error(provider, response) from None
        return parse_model_ids(provider, response)
    except (httpx.TransportError, httpx.InvalidURL) as exc:
        raise transport_error(provider, exc, produced=False, timeout=timeout) from exc
