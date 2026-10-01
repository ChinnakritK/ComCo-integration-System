# Agents

| Folder | Role | What |
|---|---|---|
| `coordinator/` | Agent API + A2A client | Plans with LangGraph, discovers specialists from their Agent Cards, delegates one A2A task per step, composes a grounded answer |
| `customer-agent/` | A2A server | Skill `get_customer` - calls the Customer API through Kong as consumer `customer-agent` |
| `order-agent/` | A2A server | Skills `get_order`, `get_latest_order`, `list_customer_orders` - Order API as consumer `order-agent` |

The specialists share one implementation, `libs/comco_agentkit/comco_agentkit/specialist.py`
(Agent Card, task lifecycle, tool execution); each folder only selects its domain.
All agents reach business data only through the Kong gateway.
Design: `docs/adr/0005-agent-architecture.md`, `docs/adr/0006-a2a.md`.
