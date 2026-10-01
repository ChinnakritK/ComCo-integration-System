"""Consistent error responses using RFC 7807 / RFC 9457 Problem Details.

Every error a ComCo service returns has the same shape and media type
(application/problem+json), so API consumers - including AI agents - can handle
failures without parsing service-specific formats:

    {
      "type": "https://api.comco.example/problems/not-found",
      "title": "Resource not found",
      "status": 404,
      "detail": "Customer C999 does not exist.",
      "instance": "/customers/C999",
      "code": "CUSTOMER_NOT_FOUND",
      "correlation_id": "4f0c...",
      "trace_id": "8a1b..."
    }
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException

from .context import current_trace_id, get_correlation_id
from .logging_config import get_logger

PROBLEM_BASE_URI = "https://api.comco.example/problems/"
PROBLEM_MEDIA_TYPE = "application/problem+json"

log = get_logger(__name__)

_DEFAULT_TITLES = {
    400: ("bad-request", "Bad request"),
    401: ("unauthorized", "Authentication required"),
    403: ("forbidden", "Access denied"),
    404: ("not-found", "Resource not found"),
    405: ("method-not-allowed", "Method not allowed"),
    409: ("conflict", "Conflict"),
    422: ("validation-error", "Request validation failed"),
    429: ("rate-limited", "Too many requests"),
    500: ("internal-error", "Internal server error"),
    503: ("dependency-unavailable", "Service dependency unavailable"),
    504: ("timeout", "Upstream timeout"),
}


class ProblemException(Exception):
    """Raise from route handlers to return a Problem Details response."""

    def __init__(
        self,
        status: int,
        detail: str,
        *,
        code: str | None = None,
        title: str | None = None,
        problem_type: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        slug, default_title = _DEFAULT_TITLES.get(status, ("error", "Error"))
        self.status = status
        self.detail = detail
        self.code = code
        self.title = title or default_title
        self.problem_type = problem_type or f"{PROBLEM_BASE_URI}{slug}"
        self.extra = extra or {}


def problem_response(
    request: Request,
    status: int,
    detail: str,
    *,
    code: str | None = None,
    title: str | None = None,
    problem_type: str | None = None,
    extra: dict[str, Any] | None = None,
) -> JSONResponse:
    slug, default_title = _DEFAULT_TITLES.get(status, ("error", "Error"))
    body: dict[str, Any] = {
        "type": problem_type or f"{PROBLEM_BASE_URI}{slug}",
        "title": title or default_title,
        "status": status,
        "detail": detail,
        "instance": request.url.path,
    }
    if code:
        body["code"] = code
    if extra:
        body.update(extra)
    body["correlation_id"] = get_correlation_id()
    trace_id = current_trace_id()
    if trace_id:
        body["trace_id"] = trace_id
    return JSONResponse(status_code=status, content=body, media_type=PROBLEM_MEDIA_TYPE)


def register_problem_handlers(app: FastAPI) -> None:
    """Map every error path of a FastAPI app onto Problem Details."""

    @app.exception_handler(ProblemException)
    async def _problem(request: Request, exc: ProblemException):
        return problem_response(
            request,
            exc.status,
            exc.detail,
            code=exc.code,
            title=exc.title,
            problem_type=exc.problem_type,
            extra=exc.extra,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError):
        errors = [
            {
                "field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
                "message": err.get("msg"),
                "type": err.get("type"),
            }
            for err in exc.errors()
        ]
        return problem_response(
            request,
            422,
            "One or more request parameters are invalid.",
            code="VALIDATION_FAILED",
            extra={"errors": errors},
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException):
        detail = exc.detail if isinstance(exc.detail, str) else "Request failed."
        return problem_response(request, exc.status_code, detail)

    @app.exception_handler(OperationalError)
    @app.exception_handler(DBAPIError)
    async def _database(request: Request, exc: Exception):
        # Never leak driver messages (hostnames, SQL) to the caller.
        log.error("database_error", error=type(exc).__name__, detail=str(exc)[:300])
        return problem_response(
            request,
            503,
            "The data store is temporarily unavailable. Please retry later.",
            code="DATA_STORE_UNAVAILABLE",
        )
