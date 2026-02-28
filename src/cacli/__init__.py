"""
cacli — Coding Agent CLI.

Provider-agnostic interface for running headless coding agents.
"""

from cacli.providers import get_provider, list_providers
from cacli.runner import (
    build_command,
    build_initial_log_entry,
    parse_output,
    run_agent,
)
from cacli.sessions import SessionInfo
from cacli.types import AgentRunResult, ExecResult, ShellExecFn

__all__ = [
    "run_agent",
    "build_command",
    "parse_output",
    "build_initial_log_entry",
    "get_provider",
    "list_providers",
    "AgentRunResult",
    "ExecResult",
    "ShellExecFn",
    "SessionInfo",
]
