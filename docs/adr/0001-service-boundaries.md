# ADR-0001: Service boundaries and data ownership

**Status:** Accepted · **Phase:** 1

## Context
Customer and order data live in separate backend systems. The platform must expose them through a
central API management layer, and later let AI agents consume them.

## Decision
* Two independent FastAPI services - **Customer Service** and **Order Service** - each with its **own
  PostgreSQL instance** (database-per-service). No shared schema, no cross-database foreign keys.
* An order references its customer **by ID only**. The Order Service does not call the Customer
  Service to validate `customer_id`; composing customer + order data is the job of the consumer
  (a client, or the agent layer in Phase 2).
* Public paths are versioned at the gateway (`/api/v1/customers`, `/api/v1/orders`); services keep
  simple internal paths (`/customers`, `/orders`). Versioning and exposure are gateway concerns.
* Backends and databases are **not published to the host**. Two Docker networks (`edge`, `backend`)
  keep the gateway away from the databases - the only way in is Kong.
* A purpose-built `GET /orders/latest?customer_id=` endpoint serves the key business question
  ("latest order of a customer") in one call with an explicit 404 `NO_ORDERS_FOR_CUSTOMER`.

## Consequences
* Services can be deployed, scaled and fail independently (demonstrated in the failure scenarios).
* A consumer may see an order for a customer ID that does not exist in the Customer Service -
  accepted for the prototype; production would use events (e.g. `CustomerDeleted`) or validation.
