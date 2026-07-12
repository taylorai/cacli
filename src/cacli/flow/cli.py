"""Argument parsing and terminal output for ``cacli flow``."""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from typing import Any

from cacli.flow import FlowBudgetExceeded, FlowCancelled, FlowRunFailed, run_flow
from cacli.flow.runs import effective_status, list_runs, load_run_args, show_run
from cacli.providers import list_providers


def add_flow_parser(subparsers: Any) -> None:
    flow = subparsers.add_parser("flow", help="Run and inspect agent workflows")
    commands = flow.add_subparsers(dest="flow_command")

    run = commands.add_parser("run", help="Run a sandboxed workflow")
    run.add_argument("script", help="Workflow Python file")
    run.add_argument("--args", default=None, help="JSON value exposed as flow_args")
    run.add_argument("--concurrency", type=int, default=None)
    run.add_argument("--provider", choices=list_providers(), default=None)
    run.add_argument("--cwd", default=".")
    run.add_argument("--budget", type=float, default=None)
    run.add_argument("--resume", default=None, metavar="RUN_ID")

    commands.add_parser("runs", help="List past workflow runs")
    show = commands.add_parser("show", help="Show a workflow run")
    show.add_argument("run_id")


def _progress(event: dict[str, Any]) -> None:
    kind = event["type"]
    if kind in {"queued", "started", "done", "failed", "cached"}:
        line = f"{kind:7} {event.get('label', 'agent')} [{event.get('provider', '?')}]"
        if "duration_ms" in event:
            line += f" {event['duration_ms'] / 1000:.1f}s"
        if event.get("cost") is not None:
            line += f" ${event['cost']:.4f}"
        if event.get("error"):
            line += f" — {event['error']}"
        print(line, flush=True)
    elif kind == "log":
        print(f"log     {event['message']}", flush=True)
    elif kind == "phase":
        print(f"phase   {event['title']}", flush=True)
    elif kind == "diverged":
        print(f"diverged at agent {event['index']}", flush=True)


async def _run(args: argparse.Namespace, flow_args: Any) -> int:
    cancel_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_handler = False
    try:
        loop.add_signal_handler(signal.SIGINT, cancel_event.set)
        installed_handler = True
    except (NotImplementedError, RuntimeError):
        pass
    try:
        outcome = await run_flow(
            args.script,
            args=flow_args,
            concurrency=args.concurrency,
            provider=args.provider,
            cwd=args.cwd,
            cancel_event=cancel_event,
            progress=_progress,
            budget=args.budget,
            resume=args.resume,
        )
    except FlowCancelled as exc:
        print(f"Flow {exc.run_id} cancelled", file=sys.stderr)
        return 130
    except FlowRunFailed as exc:
        print(f"Flow {exc.run_id} failed: {exc}", file=sys.stderr)
        return 1
    except FlowBudgetExceeded as exc:
        print(f"Flow {exc.run_id} budget exceeded: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    finally:
        if installed_handler:
            loop.remove_signal_handler(signal.SIGINT)
    print(json.dumps(outcome.result, indent=2))
    print(f"Run: {outcome.run_id}")
    return 0


def flow_command(args: argparse.Namespace) -> None:
    if args.flow_command is None:
        raise SystemExit("cacli flow requires run, runs, or show")
    if args.flow_command == "run":
        try:
            flow_args = json.loads(args.args) if args.args is not None else None
        except json.JSONDecodeError as exc:
            print(f"Invalid --args JSON: {exc}", file=sys.stderr)
            raise SystemExit(2) from exc
        if args.resume is not None:
            try:
                original_args = load_run_args(args.resume)
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                raise SystemExit(2) from exc
            if original_args != flow_args:
                print(
                    "WARNING: args differ from original run; cached results are reused "
                    "wherever call keys are unchanged",
                    file=sys.stderr,
                )
        raise SystemExit(asyncio.run(_run(args, flow_args)))
    if args.flow_command == "runs":
        for run in list_runs():
            totals = run.get("totals", {})
            print(
                f"{run.get('id')} {effective_status(run)} {run.get('name')} "
                f"agents={totals.get('agents', 0)} cost=${totals.get('cost', 0):.2f}"
                + (
                    f" (+${totals.get('cost_estimated', 0):.2f} est)"
                    if totals.get("cost_estimated", 0) > 0
                    else ""
                )
            )
        return
    data = show_run(args.run_id)
    if data is None:
        print(f"Unknown flow run: {args.run_id}", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps(data, indent=2))
