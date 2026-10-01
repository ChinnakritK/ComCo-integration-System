"""Order Agent - an A2A server that owns the order skills.

The agent's behaviour (Agent Card, task lifecycle, calling the managed Order API
through Kong) is implemented once in comco_agentkit.specialist and shared with
the other specialist; this module only selects the domain.

Endpoints:
  GET  /.well-known/agent-card.json   A2A Agent Card (discovery: name, skills, endpoint)
  POST /a2a                           A2A JSON-RPC (SendMessage, GetTask, CancelTask, ...)
"""

from comco_agentkit.specialist import create_specialist_app

app = create_specialist_app("order")
