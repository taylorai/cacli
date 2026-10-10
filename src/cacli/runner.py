"""Build and run agent CLI harnesses with explicit inference providers."""

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from cacli.harnesses import get_harness
from cacli.harnesses.pi import PiHarness
from cacli.inference import Credential, PROVIDERS
from cacli.types import AgentRunResult, ExecResult, ShellExecFn


@dataclass(frozen=True)
class LaunchSpec:
    harness: str
    provider: str
    model: str | None
    command: str
    env: dict[str, str]
    unset_env: tuple[str, ...]
    credentials: tuple[Credential, ...]


def _build_launch(
    harness: str,
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    params: dict[str, str] | None = None,
    reasoning_effort: str | None = None,
    web_search: bool = True,
    settings: str | None = None,
    sandbox: bool | None = None,
    output_schema: dict | Path | None = None,
    pr_description: bool = False,
    extensions: Sequence[str] = (),
    trust_project: bool = True,
) -> LaunchSpec:
    adapter = get_harness(harness)
    if not isinstance(adapter, PiHarness) and (extensions or not trust_project):
        raise ValueError(
            "extensions and trust_project options are supported only by the 'pi' harness"
        )
    pair = adapter.pairing(provider)
    catalog = PROVIDERS[pair.provider]
    supplied = dict(params or {})
    for param in catalog.params:
        if param.normalize and supplied.get(param.name):
            supplied[param.name] = param.normalize(supplied[param.name])
        if not supplied.get(param.name):
            raise ValueError(
                f"Missing provider parameter {param.name!r} (environment variable {param.env})"
            )
    resolved_model = adapter.resolve_model(
        model if model is not None else pair.default_model
    )
    if pair.requires_model and not resolved_model:
        raise ValueError(
            f"A model is required for {harness} with provider {pair.provider!r}"
        )
    if pair.model_prefix and resolved_model:
        resolved_model = f"{pair.model_prefix}/{resolved_model}"
    env = {key: value.format(**supplied) for key, value in pair.env}
    prefix_parts: list[str] = ["env"]
    for key in pair.unset_env:
        prefix_parts.extend(["-u", key])
    prefix_parts.extend(f"{key}={value}" for key, value in env.items())
    exec_prefix = prefix_parts if len(prefix_parts) > 1 else []
    # Only Pi takes extension and project-trust options; others were rejected above.
    pi_options = (
        {"extensions": extensions, "trust_project": trust_project}
        if isinstance(adapter, PiHarness)
        else {}
    )
    if pr_description:
        command = adapter.build_pr_description_command(
            prompt,
            model=resolved_model,
            settings=settings,
            sandbox=sandbox,
            exec_prefix=exec_prefix,
            **pi_options,
        )
    else:
        command = adapter.build_command(
            prompt,
            model=resolved_model,
            reasoning_effort=reasoning_effort or pair.default_reasoning_effort,
            web_search=web_search,
            settings=settings,
            sandbox=sandbox,
            output_schema=output_schema,
            exec_prefix=exec_prefix,
            **pi_options,
        )
    command = adapter.configure_command(pair, command, supplied)
    credentials = tuple(
        c.resolved(supplied)
        for c in catalog.credentials
        if (c.env or c.file) in pair.credentials
    )
    return LaunchSpec(
        harness,
        pair.provider,
        resolved_model,
        command,
        env,
        pair.unset_env,
        credentials,
    )


def build_launch(
    harness: str,
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    params: dict[str, str] | None = None,
    reasoning_effort: str | None = None,
    web_search: bool = True,
    settings: str | None = None,
    sandbox: bool | None = None,
    output_schema: dict | Path | None = None,
    extensions: Sequence[str] = (),
    trust_project: bool = True,
) -> LaunchSpec:
    return _build_launch(
        harness,
        prompt,
        provider=provider,
        model=model,
        params=params,
        reasoning_effort=reasoning_effort,
        web_search=web_search,
        settings=settings,
        sandbox=sandbox,
        output_schema=output_schema,
        extensions=extensions,
        trust_project=trust_project,
    )


def build_pr_description_command(
    harness: str,
    prompt: str,
    *,
    provider: str | None = None,
    model: str | None = None,
    params: dict[str, str] | None = None,
    settings: str | None = None,
    sandbox: bool | None = None,
    extensions: Sequence[str] = (),
    trust_project: bool = True,
) -> str:
    """Build a PR description invocation with the same provider selection."""
    launch = _build_launch(
        harness,
        prompt,
        provider=provider,
        model=model,
        params=params,
        settings=settings,
        sandbox=sandbox,
        extensions=extensions,
        trust_project=trust_project,
        pr_description=True,
    )
    return launch.command


def check_credentials(launch: LaunchSpec, env: dict[str, str] | None = None) -> None:
    """Check credential presence without reading or displaying secret values."""
    available = os.environ if env is None else {**os.environ, **env}
    for credential in launch.credentials:
        if (
            credential.env
            and not available.get(credential.env)
            and not credential.local_login
        ):
            raise ValueError(
                f"Missing credential environment variable {credential.env} for {launch.provider}"
            )
        file_path = credential.file
        if file_path == "~/.codex/auth.json" and available.get("CODEX_HOME"):
            file_path = str(Path(available["CODEX_HOME"]) / "auth.json")
        if file_path and not Path(file_path).expanduser().is_file():
            raise ValueError(
                f"Missing credential file {file_path} for {launch.provider}"
            )


def run_agent(
    harness: str,
    prompt: str,
    exec_fn: ShellExecFn,
    *,
    provider: str | None = None,
    model: str | None = None,
    params: dict[str, str] | None = None,
    reasoning_effort: str | None = None,
    web_search: bool = True,
    settings: str | None = None,
    sandbox: bool | None = None,
    output_schema: dict | Path | None = None,
    cwd: str = ".",
    timeout: int = 3600,
    env: dict[str, str] | None = None,
    extensions: Sequence[str] = (),
    trust_project: bool = True,
) -> AgentRunResult:
    launch = build_launch(
        harness,
        prompt,
        provider=provider,
        model=model,
        params=params,
        reasoning_effort=reasoning_effort,
        web_search=web_search,
        settings=settings,
        sandbox=sandbox,
        output_schema=output_schema,
        extensions=extensions,
        trust_project=trust_project,
    )
    exec_result: ExecResult = exec_fn(launch.command, cwd, timeout, env)
    result = get_harness(harness).parse_output(exec_result.stdout)
    result.exit_code = exec_result.exit_code or result.exit_code
    result.raw_output = exec_result.stdout
    result.harness = harness
    result.provider = launch.provider
    result.model = launch.model
    result.command = launch.command
    return result


def build_initial_log_entry(harness: str, prompt: str, model: str | None = None) -> str:
    return get_harness(harness).build_initial_log_entry(prompt, model)


def parse_output(harness: str, raw_output: str) -> AgentRunResult:
    return get_harness(harness).parse_output(raw_output)
