"""Workflow run-directory persistence and display helpers."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from cacli.flow.journal import append_journal, read_journal


def effective_status(meta: dict[str, Any]) -> str:
    """Resolve a run's display status, detecting runs whose process died.

    A killed cacli process (SIGKILL, terminal death) never writes a final
    status, leaving meta.json at "running" forever. If the recorded pid is no
    longer alive, report "interrupted" instead. The journal is intact and the
    run is resumable.
    """
    status = meta.get("status", "unknown")
    if status != "running":
        return status
    pid = meta.get("pid")
    if not isinstance(pid, int):
        return status
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "interrupted"
    except (PermissionError, OSError):
        return status
    return status


class ResultEncodingError(ValueError):
    """Raised when a workflow result cannot be represented in JSON."""


def default_runs_dir() -> Path:
    return Path.home() / ".cacli" / "flows"


def generate_run_id() -> str:
    return f"fl_{uuid.uuid4().hex[:8]}"


def _git_head(cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def normalize_json(value: Any, path: str = "result") -> Any:
    """Convert tuples recursively and reject Monty-only/non-JSON values."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, tuple):
        return [normalize_json(item, f"{path}[]") for item in value]
    if isinstance(value, list):
        return [normalize_json(item, f"{path}[]") for item in value]
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ResultEncodingError(f"{path} has non-string dict key {key!r}")
            output[key] = normalize_json(item, f"{path}.{key}")
        return output
    raise ResultEncodingError(
        f"{path} contains non-JSON value of type {type(value).__name__}"
    )


def label_slug(label: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", label).strip("-").lower()
    return (slug or "agent")[:60]


class FlowRun:
    """Mutable append-only state for one workflow execution."""

    def __init__(self, path: Path, meta: dict[str, Any]):
        self.path = path
        self.id = path.name
        self.meta = meta

    @classmethod
    def create(
        cls,
        script: Path,
        args: Any,
        meta: dict[str, Any],
        cwd: Path,
        *,
        root: Path | None = None,
        budget: float | None = None,
        resumed_from: str | None = None,
    ) -> "FlowRun":
        base = root or default_runs_dir()
        base.mkdir(parents=True, exist_ok=True)
        path = base / generate_run_id()
        path.mkdir()
        (path / "agents").mkdir()
        (path / "script.py").write_text(script.read_text())
        (path / "args.json").write_text(json.dumps(args, indent=2))
        now = time.time()
        run_meta = {
            "id": path.name,
            "name": meta.get("name", script.stem),
            "start_time": now,
            "end_time": None,
            "status": "running",
            "totals": {
                "agents": 0,
                "succeeded": 0,
                "failed": 0,
                "cost": 0.0,
                "cost_estimated": 0.0,
                "budget": budget,
                "replayed_cost": 0.0,
            },
            "cwd": str(cwd),
            "git_head": _git_head(cwd),
            "pid": os.getpid(),
        }
        if resumed_from is not None:
            run_meta["resumed_from"] = resumed_from
        obj = cls(path, run_meta)
        obj.save_meta()
        return obj

    def save_meta(self) -> None:
        (self.path / "meta.json").write_text(json.dumps(self.meta, indent=2))

    def finish(self, status: str, *, error: str | None = None) -> None:
        self.meta["status"] = status
        self.meta["end_time"] = time.time()
        if error is not None:
            self.meta["error"] = error
        self.save_meta()

    def append_jsonl(self, filename: str, entry: dict[str, Any]) -> None:
        with (self.path / filename).open("a") as handle:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")

    def event(self, event: dict[str, Any]) -> None:
        event.setdefault("time", time.time())
        self.append_jsonl("events.jsonl", event)

    def journal(self, entry: dict[str, Any]) -> None:
        append_journal(self.path / "journal.jsonl", entry)

    def agent_log_path(self, index: int, label: str) -> Path:
        return self.path / "agents" / f"{index:04d}-{label_slug(label)}.log"

    def write_result(self, result: Any) -> Any:
        normalized = normalize_json(result)
        try:
            encoded = json.dumps(normalized, indent=2, allow_nan=False)
        except ValueError as exc:
            raise ResultEncodingError(f"result is not strict JSON: {exc}") from exc
        (self.path / "result.json").write_text(encoded)
        return normalized


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def list_runs(root: Path | None = None) -> list[dict[str, Any]]:
    base = root or default_runs_dir()
    if not base.exists():
        return []
    runs = []
    for path in base.glob("fl_*/meta.json"):
        try:
            runs.append(_read_json(path))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(runs, key=lambda item: item.get("start_time", 0), reverse=True)


def show_run(run_id: str, root: Path | None = None) -> dict[str, Any] | None:
    base = root or default_runs_dir()
    path = base / run_id
    if not path.is_dir():
        return None
    try:
        output = {"meta": _read_json(path / "meta.json")}
    except (OSError, json.JSONDecodeError):
        return None
    for name in ("events", "journal"):
        file = path / f"{name}.jsonl"
        output[name] = read_journal(file)
    result_path = path / "result.json"
    if result_path.exists():
        output["result"] = _read_json(result_path)
    return output


def load_run_args(run_id: str, root: Path | None = None) -> Any:
    """Load the arguments persisted for an existing run."""
    base = root or default_runs_dir()
    path = base / run_id / "args.json"
    if not path.is_file():
        raise ValueError(f"unknown flow run: {run_id}")
    try:
        return _read_json(path)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid args for flow run: {run_id}") from exc
