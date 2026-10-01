# ADR-0004: Observability - OpenTelemetry, Jaeger, correlation IDs, JSON logs

**Status:** Accepted · **Phase:** 1

## Decision
Three complementary signals, all keyed by the same identifiers:

| Signal | Tool | How to use it |
|---|---|---|
| **Traces** | OpenTelemetry SDK -> OTLP/HTTP -> Jaeger v2 | Kong starts the trace and injects W3C `traceparent`; FastAPI + SQLAlchemy (and later httpx in the agents) continue it. One trace shows gateway -> service -> SQL. |
| **Logs** | structlog JSON to stdout; Kong `file-log` JSON | Every line has `service`, `correlation_id`, `trace_id`. `docker compose logs | grep <id>` reconstructs a request. |
| **Metrics** | Kong `prometheus` plugin | `:8100/metrics` - requests by status/consumer/route, latency, upstream health. |

* The **correlation ID** is the human-friendly handle (returned to the client in `X-Correlation-ID`
  and in every error body); the **trace ID** is the machine handle for Jaeger. Backend spans carry
  the tag `comco.correlation_id`, so Jaeger can be searched by either.
* Sampling is 100% - appropriate for a demo, not for production traffic.

## Why not the full Grafana stack?
Loki/Tempo/Grafana would add several containers and setup time without demonstrating additional
concepts. Prometheus + Grafana dashboards remain an optional extension.
