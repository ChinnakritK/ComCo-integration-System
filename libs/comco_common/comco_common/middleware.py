"""Per-request middleware: correlation ID, access log, demo fault injection."""

from __future__ import annotations

import asyncio
import os
import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

from .context import CORRELATION_HEADER, reset_correlation_id, set_correlation_id
from .errors import problem_response
from .logging_config import get_logger

log = get_logger("access")

_QUIET_PATHS = {"/health", "/ready"}


def _fault_injection_enabled() -> bool:
    return os.getenv("FAULT_INJECTION_ENABLED", "false").lower() == "true"


def _tag_current_span(correlation_id: str, consumer: str | None) -> None:
    """Make traces searchable in Jaeger by correlation ID (tag comco.correlation_id)."""
    try:
        from opentelemetry import trace

        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("comco.correlation_id", correlation_id)
            if consumer:
                span.set_attribute("comco.consumer", consumer)
    except Exception:  # pragma: no cover
        pass


class RequestContextMiddleware(BaseHTTPMiddleware):
    """
    * Accepts the correlation ID set by the API gateway (or generates one when a
      service is called directly) and echoes it on the response.
    * Writes exactly one structured access-log line per request.
    * Converts any unhandled exception into a Problem Details 500 so that even
      crashes carry the correlation ID back to the caller.
    * Demo only - when FAULT_INJECTION_ENABLED=true the headers
      `X-Simulate-Delay-Ms` and `X-Simulate-Status` let the evaluator trigger
      timeouts and downstream errors on demand (see docs / demo script).
    """

    async def dispatch(self, request: Request, call_next):
        correlation_id = request.headers.get(CORRELATION_HEADER) or str(uuid.uuid4())
        token = set_correlation_id(correlation_id)
        structlog.contextvars.bind_contextvars(correlation_id=correlation_id)
        _tag_current_span(correlation_id, request.headers.get("X-Consumer-Username"))
        started = time.perf_counter()
        status = 500
        try:
            if _fault_injection_enabled():
                delay_ms = request.headers.get("X-Simulate-Delay-Ms")
                if delay_ms and delay_ms.isdigit():
                    log.warning("fault_injection_delay", delay_ms=int(delay_ms))
                    await asyncio.sleep(min(int(delay_ms), 30_000) / 1000)
                sim_status = request.headers.get("X-Simulate-Status")
                if sim_status and sim_status.isdigit() and 400 <= int(sim_status) <= 599:
                    log.warning("fault_injection_status", status=int(sim_status))
                    response = problem_response(
                        request,
                        int(sim_status),
                        "Simulated failure (fault injection enabled for demo).",
                        code="SIMULATED_FAILURE",
                    )
                    status = response.status_code
                    response.headers[CORRELATION_HEADER] = correlation_id
                    return response

            try:
                response = await call_next(request)
            except Exception:
                log.exception("unhandled_exception")
                response = problem_response(
                    request, 500, "An unexpected error occurred.", code="INTERNAL_ERROR"
                )
            status = response.status_code
            response.headers[CORRELATION_HEADER] = correlation_id
            return response
        finally:
            if request.url.path not in _QUIET_PATHS:
                log.info(
                    "http_request",
                    method=request.method,
                    path=request.url.path,
                    query=str(request.url.query) or None,
                    status=status,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    consumer=request.headers.get("X-Consumer-Username"),
                )
            structlog.contextvars.unbind_contextvars("correlation_id")
            reset_correlation_id(token)
