# ADR-0005: Agent architecture - LangGraph Coordinator over managed APIs

**Status:** Accepted · **Phase:** 2

## Context
Requirement 4.3: an agent must understand a request, determine the needed capabilities, and invoke
the appropriate APIs/tools - single tool, multi-tool orchestration, and graceful handling of
unavailable or nonexistent data - **using managed APIs, never the databases**, and **never
fabricating** business information. A commercial LLM must not be required.

## Decision

### 1. The agent is just another API consumer
The Coordinator calls `http://kong:8000/api/v1/...` with its own API key (`agent-platform`, ACL group
`agents`, 300 req/min). Agent traffic is therefore authenticated, rate-limited, logged and traced
exactly like any client's. It has no database credentials and no network route to the databases.

### 2. Explicit LangGraph state machine
```
START -> plan_request -> execute_step (loops once per planned tool) -> compose_answer -> END
                    \-----------------(nothing to call)-----------------/
```
* **plan_request** - intent + entity extraction -> an ordered plan of tool calls with a `reason`
  and `depends_on` for each step.
* **execute_step** - runs one tool; *before* calling it checks dependencies. If `get_customer`
  returned not-found, `get_latest_order` is skipped (no point asking for orders of a customer who
  doesn't exist, and no risk of implying one). This is the conditional orchestration.
* **compose_answer** - template-based text built **only** from tool results.

The response exposes `steps` (tool, args, reason, outcome, HTTP status, latency) and `graph_path`,
so the decision-making is visible to the evaluator and in traces (one span per node/tool).

### 3. Planning is separated from answering
| | RulePlanner (default) | LlmPlanner (optional, `USE_LLM=true`, Ollama) |
|---|---|---|
| Decides | which tools, which IDs | which tools, which IDs |
| Guardrails | deterministic | output validated: known tool, argument patterns, **IDs must appear in the user's text**; dependency ordering enforced |
| On failure | - | falls back to RulePlanner, reason reported in `planner` |

Neither planner writes the answer. That is the core anti-fabrication measure: a model may choose
*what to look up*, but the business facts in the answer come verbatim from API responses.

### 4. Tool registry (`libs/comco_agentkit`)
Four tools, each wrapping one managed API operation: `get_customer`, `get_latest_order`,
`list_customer_orders`, `get_order`. Each has a JSON-schema, an `owner` domain (customer/order) and
returns a normalised `ToolResult.outcome`: `ok | not_found | unavailable | rate_limited |
unauthorized | invalid | error`. The registry doubles as capability discovery
(`GET /api/v1/agent/capabilities`) and will feed the A2A Agent Cards in Phase 3.

### 5. Response validation (contracts)
Planning cannot invent data, but an agent could still *pass on* bad data. So every successful
tool result is validated in `comco_agentkit/contracts.py` before use:

| Check | Example rejection |
|---|---|
| Schema - required fields, types, enums, ID formats, money as decimal, quantity as integer | missing `status`; `tier: "DIAMOND"` |
| Identity - the data answers the request | asked for C001, received C002; latest order belongs to C004 |
| Consistency | an order list containing another customer's order; `total` < items returned |
| Normalisation - unknown fields dropped | `internal_note` never reaches the answer |

Failures become outcome `error` / `INVALID_UPSTREAM_RESPONSE`; dependent steps are skipped and the
answer states that the data could not be verified. In A2A mode the check runs twice: inside the
specialist (before it completes the task) and again in the Coordinator on the returned artifact
(`INVALID_AGENT_RESPONSE`), which must also match the delegated tool and arguments - the
Coordinator does not trust other agents blindly.

### 6. Failure semantics
| Situation | Agent behaviour | `status` |
|---|---|---|
| Everything retrieved | full answer | `completed` |
| Customer exists, has no orders | profile + "has no orders on record" | `completed_with_gaps` |
| Entity does not exist | "C999 was not found", no data | `not_found` |
| Data failed validation | treated like a failed call - not shown | `partial` / `failed` |
| Some APIs failed | what was retrieved + "could not retrieve X because ... (HTTP 503)" | `partial` |
| Nothing retrievable | explanation only | `failed` |
| No ID in the request | asks for a customer/order ID | `needs_clarification` |
| Out of scope | lists what it can do | `unsupported` |

Transient 502/503 and connection errors are retried once (tenacity, exponential back-off); 504 and
4xx are not retried. Per-tool timeout 8 s; the Kong route for the agent allows 30 s overall.

The Agent API returns **HTTP 200 for any handled request** - the business outcome is in `status`.
A partially answered question is still a successful conversation turn; HTTP errors are reserved
for the Agent API itself (401/403/429 at the gateway, 422 invalid body, 5xx agent failure).

### 7. Phase 3 seam
Tool execution goes through a `ToolExecutor` interface. Phase 2 uses `DirectApiExecutor`
(coordinator -> Kong). Phase 3 adds an A2A executor that delegates each step to the Customer Agent
or Order Agent according to the tool's `owner` - the graph, planner and answer composer are reused.

## Consequences
* Fully deterministic demo; evaluators need no model, GPU or API key.
* The rule planner covers the assessment's phrasings and close variants, not arbitrary language -
  that is what the optional LLM planner is for.
* Steps run sequentially for readability; independent steps (e.g. two order IDs) could run in
  parallel with LangGraph's fan-out in production.
