"""Structured JSON logging.

Each log line is one JSON object carrying `service`, `correlation_id` and
`trace_id`, so `docker compose logs | grep <correlation-id>` reconstructs a
request even without opening Jaeger.
"""

from __future__ import annotations

import logging
import os
import sys

import structlog

from .context import current_trace_id, get_correlation_id

_configured = False


def _add_request_context(_, __, event_dict):
    cid = get_correlation_id()
    if cid and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    tid = current_trace_id()
    if tid and "trace_id" not in event_dict:
        event_dict["trace_id"] = tid
    return event_dict


def configure_logging(service_name: str, level: str | None = None) -> None:
    global _configured
    level_name = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
    log_level = getattr(logging, level_name, logging.INFO)

    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level, force=True)
    # Our middleware writes one access line per request; silence the duplicate.
    logging.getLogger("uvicorn.access").disabled = True

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _add_request_context,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )
    structlog.contextvars.bind_contextvars(service=service_name)
    _configured = True


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
