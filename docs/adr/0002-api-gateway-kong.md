# ADR-0002: API gateway - Kong Gateway OSS (DB-less)

**Status:** Accepted · **Phase:** 1

## Context
The assessment requires routing, authentication/access control, rate limiting, request
correlation/tracing and logging/monitoring at a central gateway, using open-source technology.

## Options considered
| Option | Fit | Drawback |
|---|---|---|
| **Kong Gateway OSS** | Every required capability is a bundled plugin; declarative config; widely used in API-management roles | From 3.10 Kong no longer publishes OSS images - 3.9.x is the last OSS line |
| Apache APISIX | Equally complete plugin set, fully open source | Requires etcd (or standalone mode), less familiar to reviewers |
| Envoy | Excellent tracing | Low-level config; API keys + consumers need extra components |
| Traefik | Simplest routing | Weak consumer / API-key management |

## Decision
Kong Gateway OSS **3.9.1** (image `kong:3.9.1`) in **DB-less mode** with a single declarative file
`gateway/kong.yml`.

| Requirement | Kong implementation |
|---|---|
| Routing | Services + routes, versioned public paths, upstreams with active health checks |
| Authentication | `key-auth` (header `apikey`, `hide_credentials: true`) |
| Access control | `acl` per service (groups `external-clients`, `agents`, `order-readers`) |
| Rate limiting | Global `rate-limiting` 60/min per consumer; consumer-scoped tiers (trial 5/min, agents 300/min) |
| Correlation | `correlation-id` (`X-Correlation-ID`, echoed downstream) |
| Tracing | `opentelemetry` -> Jaeger, W3C `traceparent` injected upstream |
| Monitoring | `prometheus` (status API `:8100/metrics`), `file-log` JSON access log |
| Consistent errors | Custom JSON error template for gateway-generated errors |

## Consequences
* Gateway configuration is code: reviewed in Git, reproducible, no admin clicks.
* DB-less means the Admin API is read-only; changing config = edit file + restart/reload.
* `policy: local` rate-limit counters are per Kong node - fine for one node; production with several
  nodes would use `redis` policy.
* Licensing risk is acknowledged: pinning 3.9.x is fine for a prototype; for production we would
  re-evaluate (Kong Enterprise/Konnect, or migrate the same concepts to APISIX/Envoy Gateway).
