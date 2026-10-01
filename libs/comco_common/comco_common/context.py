"""Request-scoped context shared by logging, error handling and outbound calls."""

from contextvars import ContextVar

CORRELATION_HEADER = "X-Correlation-ID"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """Correlation ID of the request currently being handled (None outside a request)."""
    return _correlation_id.get()


def set_correlation_id(value: str | None):
    return _correlation_id.set(value)


def reset_correlation_id(token) -> None:
    _correlation_id.reset(token)


def current_trace_id() -> str | None:
    """W3C trace ID of the active OpenTelemetry span, if tracing is on."""
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            return format(ctx.trace_id, "032x")
    except Exception:  # pragma: no cover - tracing must never break a request
        pass
    return None
