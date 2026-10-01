from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: str) -> bool:
    return os.getenv(name, default).lower() == "true"


@dataclass(frozen=True)
class Settings:
    service_name: str = "coordinator-agent"
    gateway_url: str = "http://kong:8000/api/v1"
    agent_api_key: str = ""
    tool_timeout_seconds: float = 8.0
    use_llm: bool = False
    ollama_url: str = "http://ollama:11434"
    ollama_model: str = "llama3.2:3b"
    llm_timeout_seconds: float = 20.0
    fault_injection_enabled: bool = False
    # "a2a": delegate each step to the specialist agent advertising the skill (Phase 3)
    # "direct": call the managed APIs from the coordinator itself (Phase 2)
    execution_mode: str = "direct"
    a2a_agent_urls: tuple[str, ...] = ()
    a2a_timeout_seconds: float = 12.0

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            service_name=os.getenv("SERVICE_NAME", cls.service_name),
            gateway_url=os.getenv("GATEWAY_URL", cls.gateway_url),
            agent_api_key=os.getenv("AGENT_API_KEY", cls.agent_api_key),
            tool_timeout_seconds=float(os.getenv("TOOL_TIMEOUT_SECONDS", cls.tool_timeout_seconds)),
            use_llm=_bool("USE_LLM", "false"),
            ollama_url=os.getenv("OLLAMA_URL", cls.ollama_url),
            ollama_model=os.getenv("OLLAMA_MODEL", cls.ollama_model),
            llm_timeout_seconds=float(os.getenv("LLM_TIMEOUT_SECONDS", cls.llm_timeout_seconds)),
            fault_injection_enabled=_bool("FAULT_INJECTION_ENABLED", "false"),
            execution_mode=os.getenv("EXECUTION_MODE", cls.execution_mode).lower(),
            a2a_agent_urls=tuple(u.strip() for u in os.getenv("A2A_AGENT_URLS", "").split(",") if u.strip()),
            a2a_timeout_seconds=float(os.getenv("A2A_TIMEOUT_SECONDS", cls.a2a_timeout_seconds)),
        )
