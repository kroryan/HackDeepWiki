"""Stable access point for the OpenCode adapter."""

from __future__ import annotations

from api.code_agent.manager import manager as _manager
from api.code_agent.port import CodeAgentPort


def get_code_agent() -> CodeAgentPort:
    return _manager


code_agent: CodeAgentPort = _manager
