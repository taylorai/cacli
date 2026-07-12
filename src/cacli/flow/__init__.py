"""Public workflow-engine API."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from cacli.flow.engine import FlowEngine
from cacli.flow.host import FlowHost, default_concurrency
from cacli.flow.journal import ResumeCache
from cacli.flow.runs import (
    FlowRun,
    ResultEncodingError,
    default_runs_dir,
    normalize_json,
)
from cacli.flow.sandbox import FlowScriptError, execute, extract_meta
from cacli.providers import list_providers


@dataclass
class FlowOutcome:
    run_id: str
    result: Any
    status: str
    path: Path


class FlowCancelled(RuntimeError):
    def __init__(self, run_id: str, message: str = "workflow cancelled"):
        super().__init__(message)
        self.run_id = run_id


class FlowRunFailed(RuntimeError):
    def __init__(self, run_id: str, message: str):
        super().__init__(message)
        self.run_id = run_id


class FlowBudgetExceeded(RuntimeError):
    def __init__(self, run_id: str, message: str):
        super().__init__(message)
        self.run_id = run_id


def _bounded_int(name: str, value: Any, low: int, high: int) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not low <= value <= high
    ):
        raise ValueError(f"{name} must be an integer from {low} to {high}")
    return value


def _budget(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("budget must be a non-negative finite number")
    result = float(value)
    if result < 0 or not math.isfinite(result):
        raise ValueError("budget must be a non-negative finite number")
    return result


async def run_flow(
    script: str | Path,
    *,
    args: Any = None,
    concurrency: int | None = None,
    provider: str | None = None,
    cwd: str | Path | None = None,
    runs_root: Path | None = None,
    cancel_event: asyncio.Event | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
    engine_options: dict[str, Any] | None = None,
    limits: dict[str, Any] | None = None,
    budget: float | None = None,
    resume: str | None = None,
) -> FlowOutcome:
    """Execute one sandboxed workflow in the foreground."""
    script_path = Path(script).resolve()
    if not script_path.is_file():
        raise FileNotFoundError(f"workflow script not found: {script}")
    source = script_path.read_text()
    meta = extract_meta(source, str(script_path))
    name = meta.get("name", script_path.stem)
    if not isinstance(name, str) or not name:
        raise ValueError("META.name must be a non-empty string")
    normalized_args = normalize_json(args, path="args")
    flow_cwd = Path(cwd or Path.cwd()).resolve()
    if not flow_cwd.is_dir():
        raise ValueError(f"flow cwd is not a directory: {flow_cwd}")

    configured_concurrency = (
        concurrency
        if concurrency is not None
        else meta.get("concurrency", default_concurrency())
    )
    configured_concurrency = _bounded_int("concurrency", configured_concurrency, 1, 64)
    max_agents = _bounded_int("max_agents", meta.get("max_agents", 500), 1, 100_000)
    default_provider = provider or meta.get("default_provider", "claude")
    if default_provider not in list_providers():
        raise ValueError(f"unknown default provider: {default_provider}")

    configured_budget = _budget(budget if budget is not None else meta.get("budget"))
    resume_cache = None
    if resume is not None:
        resume_path = (runs_root or default_runs_dir()) / resume
        if not resume_path.is_dir():
            raise ValueError(f"unknown flow run: {resume}")
        resume_cache = ResumeCache.from_journal(resume_path / "journal.jsonl")

    run = FlowRun.create(
        script_path,
        normalized_args,
        meta,
        flow_cwd,
        root=runs_root,
        budget=configured_budget,
        resumed_from=resume,
    )
    cancellation = cancel_event or asyncio.Event()
    options = dict(engine_options or {})
    engine = FlowEngine(
        run,
        concurrency=configured_concurrency,
        max_agents=max_agents,
        cancel_event=cancellation,
        progress=progress,
        budget=configured_budget,
        resume_cache=resume_cache,
        **options,
    )
    host = FlowHost(engine, cwd=flow_cwd, default_provider=default_provider)
    engine.start()
    try:
        result = await execute(
            source,
            str(script_path),
            normalized_args,
            host,
            limits=limits,
        )
        normalized = run.write_result(result)
    except FlowScriptError as exc:
        message = str(exc)
        if "FlowCancelled:" in message or cancellation.is_set():
            run.finish("cancelled", error=message)
            raise FlowCancelled(run.id) from exc
        if "FlowBudgetExceeded:" in message:
            run.finish("budget_exceeded", error=message)
            raise FlowBudgetExceeded(run.id, message) from exc
        run.finish("failed", error=message)
        raise FlowRunFailed(run.id, message) from exc
    except ResultEncodingError as exc:
        run.finish("failed", error=str(exc))
        raise FlowRunFailed(run.id, str(exc)) from exc
    except asyncio.CancelledError:
        cancellation.set()
        run.finish("cancelled", error="workflow task cancelled")
        raise
    except Exception as exc:
        run.finish("failed", error=str(exc))
        raise FlowRunFailed(run.id, str(exc)) from exc
    else:
        run.finish("completed")
        return FlowOutcome(run.id, normalized, "completed", run.path)
    finally:
        await engine.close()


__all__ = [
    "FlowCancelled",
    "FlowBudgetExceeded",
    "FlowHost",
    "FlowOutcome",
    "FlowRunFailed",
    "run_flow",
]
