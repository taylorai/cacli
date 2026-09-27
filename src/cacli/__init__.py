"""
cacli — Coding Agent CLI.

Harness-agnostic interface for running headless coding agents.
"""

from cacli.harnesses import get_harness, list_harnesses
from cacli.inference import Credential, Param, Provider, WireBinding
from cacli.runner import (
    LaunchSpec,
    build_launch,
    build_initial_log_entry,
    build_pr_description_command,
    parse_output,
    run_agent,
)
from cacli.sessions import SessionInfo
from cacli.types import AgentRunResult, ExecResult, ShellExecFn

__all__ = [
    "run_agent",
    "build_launch",
    "parse_output",
    "build_initial_log_entry",
    "build_pr_description_command",
    "get_harness",
    "list_harnesses",
    "AgentRunResult",
    "ExecResult",
    "ShellExecFn",
    "SessionInfo",
    "LaunchSpec",
    "Provider",
    "Param",
    "Credential",
    "WireBinding",
]
