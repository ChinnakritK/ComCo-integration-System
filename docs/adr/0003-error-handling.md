# ADR-0003: One error format - RFC 7807 Problem Details

**Status:** Accepted · **Phase:** 1

## Decision
Every error from every service is `application/problem+json`:

```json
{
  "type": "https://api.comco.example/problems/not-found",
  "title": "Resource not found",
  "status": 404,
  "detail": "Customer C999 does not exist.",
  "instance": "/customers/C999",
  "code": "CUSTOMER_NOT_FOUND",
  "correlation_id": "0b9c...",
  "trace_id": "4bf9..."
}
```

* `code` is a stable, machine-readable value - agents branch on it instead of parsing text.
* `correlation_id` / `trace_id` let support staff jump straight to logs and the trace.
* Validation errors (422) add an `errors[]` array with `field`, `message`, `type`.
* Database failures map to **503 `DATA_STORE_UNAVAILABLE`** without leaking driver details.
* Unhandled exceptions map to **500 `INTERNAL_ERROR`**, still with the correlation ID.
* Gateway-generated errors (401/403/429/502/503/504) use a Kong JSON error template with the same
  `type`/`title`/`detail` fields plus `request_id` and `source: kong-gateway`.

## Status codes used
200, 201 (+`Location`), 401, 403, 404, 409 (duplicate email, invalid order status transition),
422, 429, 500, 502/503 (dependency unavailable), 504 (timeout).

## Known gap
Kong's template can only substitute the message and request ID, so gateway errors have no `status`
field in the body and are served as `application/json`. A small custom plugin or response
transformer would close this in production.
