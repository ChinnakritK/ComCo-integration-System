"""Shared cross-cutting concerns for ComCo services.

Every service (backend APIs and, later, agents) uses the same building blocks so
that error payloads, log lines and traces look identical across the platform.
"""

from .context import get_correlation_id
from .errors import ProblemException, register_problem_handlers
from .logging_config import configure_logging, get_logger
from .middleware import RequestContextMiddleware
from .telemetry import setup_telemetry

__all__ = [
    "ProblemException",
    "RequestContextMiddleware",
    "configure_logging",
    "get_correlation_id",
    "get_logger",
    "register_problem_handlers",
    "setup_telemetry",
]
