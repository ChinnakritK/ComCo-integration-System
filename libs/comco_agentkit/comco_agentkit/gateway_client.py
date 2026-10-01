"""HTTP client for calling business APIs through the Kong gateway.

Design points:
* The agent authenticates to Kong as its own consumer (`agent-platform`, API key
  from AGENT_API_KEY) - it is subject to the same auth, ACL, rate limits and
  logging as any other API consumer.
* The current correlation ID is forwarded, so Kong keeps one ID for the whole
  user request (Kong's correlation-id plugin preserves an incoming header).
* W3C `traceparent` is injected by the OpenTelemetry httpx instrumentation.
* Transient failures (connection errors, 502, 503) are retried once with
  back-off; 4xx and 504 are not retried (retrying a timeout doubles latency).
"""

from __future__ import annotations

import os
from contextvars import ContextVar

import httpx
from tenacity import (
    AsyncRetrying,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

_RETRYABLE_STATUS = {502, 503}

# Demo-only fault injection: the coordinator copies these onto downstream calls
# for one target service (see coordinator middleware).
_fault_headers: ContextVar[dict[str, dict[str, str]] | None] = ContextVar("fault_headers", default=None)


def downstream_fault_headers() -> ContextVar:
    return _fault_headers


class _RetryableStatus(Exception):
    def __init__(self, response: httpx.Response):
        self.response = response


class GatewayClient:
    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        max_attempts: int = 2,
    ) -> None:
        self.base_url = (base_url or os.getenv("GATEWAY_URL", "http://kong:8000/api/v1")).rstrip("/")
        self._api_key = api_key or os.getenv("AGENT_API_KEY", "")
        timeout = timeout_seconds or float(os.getenv("TOOL_TIMEOUT_SECONDS", "8"))
        self.max_attempts = max_attempts
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(timeout, connect=2.0),
            transport=transport,
            headers={"apikey": self._api_key, "User-Agent": "comco-agent/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get(
        self,
        path: str,
        *,
        params: dict | None = None,
        correlation_id: str | None = None,
        target: str | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        if correlation_id:
            headers["X-Correlation-ID"] = correlation_id
        faults = _fault_headers.get() or {}
        if target and target in faults:
            headers.update(faults[target])

        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self.max_attempts),
                wait=wait_exponential(multiplier=0.3, max=2),
                retry=retry_if_exception_type((httpx.TransportError, _RetryableStatus)),
                reraise=False,
            ):
                with attempt:
                    response = await self._client.get(path, params=params, headers=headers)
                    if response.status_code in _RETRYABLE_STATUS:
                        raise _RetryableStatus(response)
                    return response
        except RetryError as exc:
            last = exc.last_attempt.exception()
            if isinstance(last, _RetryableStatus):
                return last.response
            raise last from exc
        raise RuntimeError("unreachable")  # pragma: no cover
