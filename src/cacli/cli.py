"""CLI entrypoint for cacli."""

import argparse
import json
import os
import subprocess
import sys

from cacli.providers import list_providers
from cacli.runner import build_command, run_agent
from cacli.types import ExecResult


def subprocess_exec(
    cmd: str, cwd: str, timeout: int, env: dict[str, str] | None
) -> ExecResult:
    """Default exec_fn using subprocess."""
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=timeout,
            env=full_env,
        )
        return ExecResult(
            exit_code=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except subprocess.TimeoutExpired:
        return ExecResult(exit_code=124, stdout="", stderr="Command timed out")


def _normalize_argv(argv: list[str]) -> list[str]:
    """Prepend 'run' if first arg isn't a known subcommand.

    Preserves backward compat: ``cacli "my prompt"`` works as ``cacli run "my prompt"``.
    """
    if not argv:
        return argv
    first = argv[0]
    known = {"run", "spawn", "status", "dashboard"}
    if first in known or first in ("-h", "--help", "help") or first.startswith("-"):
        return argv
    return ["run", *argv]


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared between run and spawn."""
    parser.add_argument("prompt", help="The task prompt")
    parser.add_argument(
        "--provider",
        "-p",
        default="claude",
        choices=list_providers(),
        help="Agent provider (default: claude)",
    )
    parser.add_argument("--model", "-m", default=None, help="Model name")
    parser.add_argument(
        "--reasoning-effort",
        "-e",
        default=None,
        choices=["low", "medium", "high", "max", "xhigh"],
        help="Reasoning effort level",
    )
    parser.add_argument(
        "--settings",
        "-s",
        default=None,
        help="Path to settings file (forwarded to claude --settings)",
    )
    parser.add_argument(
        "--no-web-search",
        dest="web_search",
        action="store_false",
        default=True,
    )
    parser.add_argument("--cwd", default=".", help="Working directory")
    parser.add_argument(
        "--timeout", "-t", type=int, default=3600, help="Timeout in seconds"
    )


def _run_command(args) -> None:
    """Handle 'cacli run' — synchronous agent execution."""
    if args.command_only:
        cmd = build_command(
            args.provider,
            args.prompt,
            model=args.model,
            reasoning_effort=args.reasoning_effort,
            web_search=args.web_search,
            settings=args.settings,
        )
        print(cmd)
        return

    result = run_agent(
        provider=args.provider,
        prompt=args.prompt,
        exec_fn=subprocess_exec,
        model=args.model,
        reasoning_effort=args.reasoning_effort,
        web_search=args.web_search,
        settings=args.settings,
        cwd=args.cwd,
        timeout=args.timeout,
    )

    if args.raw:
        print(result.raw_output)
    elif args.json:
        print(
            json.dumps(
                {
                    "exit_code": result.exit_code,
                    "result_message": result.result_message,
                    "total_cost": result.total_cost,
                    "provider": result.provider,
                    "model": result.model,
                    "command": result.command,
                },
                indent=2,
            )
        )
    else:
        if result.result_message:
            print(result.result_message)
        else:
            print(result.raw_output)

    sys.exit(result.exit_code)


def _spawn_command(args) -> None:
    """Handle 'cacli spawn' — launch agent in tmux."""
    from cacli.spawn import spawn_agent

    spawn_agent(args)


def _status_command(_args) -> None:
    """Handle 'cacli status' — TUI dashboard."""
    from cacli.status import status_tui

    status_tui()


def _dashboard_command(args) -> None:
    """Handle 'cacli dashboard' — web dashboard."""
    from cacli.server import run_server

    run_server(port=args.port)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="cacli",
        description="Run coding agents (provider-agnostic)",
    )
    subparsers = parser.add_subparsers(dest="command")

    # cacli run <prompt>
    run_parser = subparsers.add_parser("run", help="Run agent synchronously")
    _add_common_args(run_parser)
    run_parser.add_argument(
        "--raw",
        action="store_true",
        help="Print raw output instead of parsed result",
    )
    run_parser.add_argument(
        "--command-only",
        action="store_true",
        help="Just print the command, don't execute",
    )
    run_parser.add_argument("--json", action="store_true", help="Output result as JSON")

    # cacli spawn <prompt>
    spawn_parser = subparsers.add_parser("spawn", help="Spawn agent in tmux session")
    _add_common_args(spawn_parser)
    spawn_parser.add_argument(
        "--name", "-n", default=None, help="Human-readable session name"
    )

    # cacli status
    subparsers.add_parser("status", help="Show spawned agent dashboard (TUI)")

    # cacli dashboard
    dashboard_parser = subparsers.add_parser(
        "dashboard", help="Start web dashboard on localhost"
    )
    dashboard_parser.add_argument(
        "--port", type=int, default=8420, help="Port to serve on (default: 8420)"
    )

    args = parser.parse_args(_normalize_argv(sys.argv[1:]))

    if args.command is None:
        parser.print_help()
        sys.exit(1)
    elif args.command == "run":
        _run_command(args)
    elif args.command == "spawn":
        _spawn_command(args)
    elif args.command == "status":
        _status_command(args)
    elif args.command == "dashboard":
        _dashboard_command(args)


if __name__ == "__main__":
    main()
