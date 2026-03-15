"""
agents/__init__.py — Agent registry.

Available agents:
  "claude"  — Claude Code CLI (subprocess-based)
  "simple"  — OpenAI ChatGPT (direct API call with conversation history)
"""

from __future__ import annotations

from app.agents.base import BaseAgent

__all__ = ["BaseAgent"]
