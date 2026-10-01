"""OpenTelemetry tracing setup.

Traces are exported over OTLP/HTTP (default endpoint from the standard
OTEL_EXPORTER_OTLP_ENDPOINT variable, e.g. http://jaeger:4318). The W3C
`traceparent` header injected by Kong is picked up automatically, so a single
trace spans gateway -> service -> database (and later agent -> agent).
"""

from __future__ import annotations

import os

from fastapi import FastAPI

from .logging_config import get_logger

log = get_logger(__name__)

_provider_initialised = False


def tracing_enabled() -> bool:
    return os.getenv("OTEL_ENABLED", "false").lower() == "true"


def init_tracing(service_name: str) -> bool:
    """Create the tracer provider and instrument httpx (idempotent).

    Call this BEFORE creating any httpx client that should propagate traces -
    the httpx instrumentation only affects clients created afterwards.
    """
    if not tracing_enabled():
        return False
    global _provider_initialised
    if _provider_initialised:
        return True
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create(
        {
            "service.name": service_name,
            "service.namespace": "comco",
            "deployment.environment": os.getenv("DEPLOYMENT_ENV", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    # Outbound HTTP (used by agents) propagates traceparent automatically.
    HTTPXClientInstrumentor().instrument()
    _provider_initialised = True
    return True


def setup_telemetry(app: FastAPI, service_name: str, sqlalchemy_engine=None) -> None:
    if not init_tracing(service_name):
        log.info("tracing_disabled")
        return

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,ready")

    if sqlalchemy_engine is not None:
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor

        SQLAlchemyInstrumentor().instrument(engine=sqlalchemy_engine.sync_engine)

    log.info("tracing_enabled", exporter="otlp-http")
