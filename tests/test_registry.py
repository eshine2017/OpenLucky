"""Tests for app.agents.registry — AgentRegistry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.agents.registry import AgentRegistry


def _mock_agent(name: str) -> MagicMock:
    agent = MagicMock()
    agent.name = name
    return agent


class TestGet:
    def test_returns_agent_for_known_provider(self) -> None:
        claude = _mock_agent("claude")
        registry = AgentRegistry({"claude": claude}, default="claude")
        assert registry.get("claude") is claude

    def test_returns_second_agent_for_known_provider(self) -> None:
        claude = _mock_agent("claude")
        gemini = _mock_agent("gemini")
        registry = AgentRegistry({"claude": claude, "gemini": gemini}, default="claude")
        assert registry.get("gemini") is gemini

    def test_none_falls_back_to_default(self) -> None:
        claude = _mock_agent("claude")
        registry = AgentRegistry({"claude": claude}, default="claude")
        assert registry.get(None) is claude

    def test_empty_string_falls_back_to_default(self) -> None:
        claude = _mock_agent("claude")
        registry = AgentRegistry({"claude": claude}, default="claude")
        assert registry.get("") is claude

    def test_unknown_provider_falls_back_to_default(self) -> None:
        claude = _mock_agent("claude")
        registry = AgentRegistry({"claude": claude}, default="claude")
        assert registry.get("unknown_provider") is claude

    def test_raises_when_no_agents_registered(self) -> None:
        registry = AgentRegistry({}, default="claude")
        with pytest.raises(ValueError, match="claude"):
            registry.get(None)

    def test_raises_when_default_not_in_agents(self) -> None:
        gemini = _mock_agent("gemini")
        registry = AgentRegistry({"gemini": gemini}, default="claude")
        with pytest.raises(ValueError, match="claude"):
            registry.get("unknown")


class TestAvailable:
    def test_lists_all_registered_provider_names(self) -> None:
        claude = _mock_agent("claude")
        gemini = _mock_agent("gemini")
        registry = AgentRegistry({"claude": claude, "gemini": gemini}, default="claude")
        assert set(registry.available) == {"claude", "gemini"}

    def test_single_provider(self) -> None:
        claude = _mock_agent("claude")
        registry = AgentRegistry({"claude": claude}, default="claude")
        assert registry.available == ["claude"]

    def test_empty_registry(self) -> None:
        registry = AgentRegistry({}, default="claude")
        assert registry.available == []


class TestDefault:
    def test_returns_configured_default(self) -> None:
        registry = AgentRegistry({}, default="gemini")
        assert registry.default == "gemini"

    def test_default_is_claude_by_convention(self) -> None:
        claude = _mock_agent("claude")
        registry = AgentRegistry({"claude": claude}, default="claude")
        assert registry.default == "claude"
