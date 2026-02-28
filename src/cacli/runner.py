"""Main runner — build commands, execute via caller's exec_fn, parse results."""

from cacli.providers import get_provider
from cacli.types import AgentRunResult, ExecResult, ShellExecFn


def run_agent(
    provider: str,
    prompt: str,
    exec_fn: ShellExecFn,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    web_search: bool = True,
    settings: str | None = None,
    cwd: str = ".",
    timeout: int = 3600,
    env: dict[str, str] | None = None,
) -> AgentRunResult:
    """
    Run a coding agent and return a unified result.

    The caller provides exec_fn which handles actual command execution.
    This function handles command building, output parsing, and result
    normalization across all providers.
    """
    prov = get_provider(provider)
    resolved_model = prov.resolve_model(model)

    command = prov.build_command(
        prompt=prompt,
        model=resolved_model,
        reasoning_effort=reasoning_effort,
        web_search=web_search,
        settings=settings,
    )

    exec_result: ExecResult = exec_fn(command, cwd, timeout, env)

    agent_result = prov.parse_output(exec_result.stdout)
    agent_result.exit_code = exec_result.exit_code
    agent_result.raw_output = exec_result.stdout
    agent_result.provider = provider
    agent_result.model = resolved_model
    agent_result.command = command

    return agent_result


def build_command(
    provider: str,
    prompt: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    web_search: bool = True,
    settings: str | None = None,
) -> str:
    """Build the shell command string without executing."""
    prov = get_provider(provider)
    resolved_model = prov.resolve_model(model)
    return prov.build_command(
        prompt=prompt,
        model=resolved_model,
        reasoning_effort=reasoning_effort,
        web_search=web_search,
        settings=settings,
    )


def build_initial_log_entry(
    provider: str, prompt: str, model: str | None = None
) -> str:
    """Build the initial JSONL log entry for the given provider."""
    prov = get_provider(provider)
    return prov.build_initial_log_entry(prompt, model)


def parse_output(provider: str, raw_output: str) -> AgentRunResult:
    """Parse raw output without executing."""
    prov = get_provider(provider)
    return prov.parse_output(raw_output)
