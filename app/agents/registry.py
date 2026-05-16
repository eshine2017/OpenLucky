"""agents/registry.py — Maps provider names to agent instances."""

from __future__ import annotations

import logging

from app.agents.base import BaseAgent

logger = logging.getLogger(__name__)


class AgentRegistry:
    """Thin registry mapping provider name → agent instance."""

    def __init__(self, agents: dict[str, BaseAgent], default: str = "claude") -> None:
        self._agents = agents
        self._default = default

    def get(self, provider: str | None) -> BaseAgent:
        """Return agent for *provider*, falling back to the default."""
        name = provider or self._default
        if name in self._agents:
            return self._agents[name]
        if self._default in self._agents:
            logger.warning("Provider %r not registered; falling back to %r", name, self._default)
            return self._agents[self._default]
        raise ValueError(
            f"No agent registered for {name!r} and default {self._default!r} also missing"
        )

    @property
    def available(self) -> list[str]:
        return sorted(self._agents.keys())

    @property
    def default(self) -> str:
        return self._default
