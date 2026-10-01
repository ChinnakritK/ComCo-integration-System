"""OpenAPI helpers shared by all services: Problem schema + API-key security scheme."""

from __future__ import annotations

from fastapi import FastAPI

from .errors import PROBLEM_MEDIA_TYPE

PROBLEM_SCHEMA = {
    "type": "object",
    "description": "RFC 7807 Problem Details - the single error format used across the platform.",
    "required": ["type", "title", "status", "detail"],
    "properties": {
        "type": {"type": "string", "format": "uri"},
        "title": {"type": "string"},
        "status": {"type": "integer"},
        "detail": {"type": "string"},
        "instance": {"type": "string"},
        "code": {"type": "string", "description": "Stable machine-readable error code"},
        "correlation_id": {"type": "string"},
        "trace_id": {"type": "string"},
        "errors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "field": {"type": "string"},
                    "message": {"type": "string"},
                    "type": {"type": "string"},
                },
            },
        },
    },
}


def problem_responses(**codes: str) -> dict:
    """problem_responses(r404="Not found", r422="...") -> FastAPI `responses=` mapping."""
    return {
        int(key.lstrip("r")): {
            "description": description,
            "content": {PROBLEM_MEDIA_TYPE: {"schema": {"$ref": "#/components/schemas/Problem"}}},
        }
        for key, description in codes.items()
    }


def install_openapi_extras(app: FastAPI) -> None:
    original = app.openapi

    def openapi():
        if app.openapi_schema:
            return app.openapi_schema
        schema = original()
        components = schema.setdefault("components", {})
        components.setdefault("schemas", {})["Problem"] = PROBLEM_SCHEMA
        components["securitySchemes"] = {
            "ApiKeyAuth": {
                "type": "apiKey",
                "in": "header",
                "name": "apikey",
                "description": "Consumer API key, validated by the Kong gateway (key-auth plugin).",
            }
        }
        schema["security"] = [{"ApiKeyAuth": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi
