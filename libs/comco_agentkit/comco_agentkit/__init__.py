"""Shared agent building blocks.

* tools / gateway_client - use the *managed* business APIs (through Kong), never the databases
* contracts - validate every response (schema + identity) before an agent uses it
* specialist - A2A server for the Customer Agent and Order Agent (import explicitly:
  `from comco_agentkit.specialist import create_specialist_app`)
"""

from .contracts import ContractViolation, validate_tool_data
from .gateway_client import GatewayClient, downstream_fault_headers
from .tools import TOOLS, Tool, ToolResult, execute_tool, tool_catalog

__all__ = [
    "ContractViolation",
    "validate_tool_data",
    "TOOLS",
    "GatewayClient",
    "Tool",
    "ToolResult",
    "downstream_fault_headers",
    "execute_tool",
    "tool_catalog",
]
