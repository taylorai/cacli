"""Core types for the cacli library."""

from dataclasses import dataclass, field
from typing import Callable


@dataclass
class ExecResult:
    """Result returned by the caller-provided exec_fn."""

    exit_code: int
    stdout: str
    stderr: str = ""


# The exec function signature the caller must provide.
# Args: (shell_command, cwd, timeout_seconds, env_vars)
ShellExecFn = Callable[[str, str, int, dict[str, str] | None], ExecResult]


@dataclass
class AgentRunResult:
    """Unified result from running any coding agent."""

    exit_code: int = 0
    raw_output: str = ""
    result_message: str = ""
    total_cost: float | None = None
    permission_denials: list[dict] = field(default_factory=list)
    provider: str = ""
    model: str | None = None
    command: str = ""
