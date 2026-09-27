"""Stream Devin's ATIF export as Claude-style stream-json on stdout.

Devin's ``--print`` mode only writes the final answer to stdout; the full
trajectory (reasoning, tool calls, tool results) lives in the ``--export``
file, which Devin rewrites incrementally as steps complete. This module wraps
a devin invocation: it injects ``--export``, tails the export file while
devin runs, and emits each step as Claude-headless stream-json lines so the
session log can be followed live (e.g. with ccviewer) just like the other
providers.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

POLL_INTERVAL = 1.0


def step_to_entries(step: dict, fallback_model: str | None = None) -> list[dict]:
    """Convert one ATIF step to Claude stream-json entries.

    System steps (prompt blobs) produce nothing. An agent step produces an
    assistant entry (thinking/text/tool_use blocks) and, if its observation
    is present, a user entry with tool_result blocks.
    """
    source = step.get("source")
    timestamp = step.get("timestamp")
    entries: list[dict] = []

    # Only agent steps are emitted. System steps are prompt blobs, and the
    # user step is skipped to match other providers' stream-json (the prompt
    # is the log writer's job, via build_initial_log_entry) — emitting it too
    # would duplicate the prompt in viewers.
    if source != "agent":
        return entries

    content: list[dict] = []
    reasoning = step.get("reasoning_content")
    if reasoning:
        content.append({"type": "thinking", "thinking": reasoning})
    message = step.get("message")
    if message:
        content.append({"type": "text", "text": message})
    for call in step.get("tool_calls") or []:
        content.append(
            {
                "type": "tool_use",
                "id": call.get("tool_call_id"),
                "name": call.get("function_name"),
                "input": call.get("arguments") or {},
            }
        )
    if content:
        entries.append(
            {
                "type": "assistant",
                "timestamp": timestamp,
                "message": {
                    "id": f"devin-step-{step.get('step_id')}",
                    "role": "assistant",
                    "model": step.get("model_name") or fallback_model,
                    "content": content,
                },
            }
        )

    entries.extend(observation_entries(step))
    return entries


def observation_entries(step: dict) -> list[dict]:
    """Convert an agent step's observation to a tool_result user entry."""
    observation = step.get("observation") or {}
    results = observation.get("results") or []
    if not results:
        return []
    blocks = [
        {
            "type": "tool_result",
            "tool_use_id": result.get("source_call_id"),
            "content": _observation_content(result),
        }
        for result in results
    ]
    return [
        {
            "type": "user",
            "timestamp": step.get("timestamp"),
            "message": {
                "id": f"devin-obs-{step.get('step_id')}",
                "role": "user",
                "content": blocks,
            },
        }
    ]


def _observation_content(result: dict) -> str:
    content = result.get("content")
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content)


def init_entry(export: dict, cwd: str) -> dict:
    agent = export.get("agent") or {}
    return {
        "type": "system",
        "subtype": "init",
        "harness": "devin",
        "session_id": export.get("session_id"),
        "model": agent.get("model_name"),
        "cwd": cwd,
    }


def result_entry(export: dict | None, fallback_text: str) -> dict:
    result_text = fallback_text
    num_turns = 0
    session_id = None
    usage = None
    if export:
        session_id = export.get("session_id")
        usage = export.get("final_metrics")
        agent_steps = [
            s for s in export.get("steps") or [] if s.get("source") == "agent"
        ]
        num_turns = len(agent_steps)
        for step in reversed(agent_steps):
            if step.get("message"):
                result_text = step["message"]
                break
    entry = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "num_turns": num_turns,
        "result": result_text,
        "session_id": session_id,
    }
    if usage:
        entry["usage"] = usage
    return entry


class ExportTailer:
    """Tracks which export steps/observations have already been emitted."""

    def __init__(self, export_path: str, cwd: str, emit) -> None:
        self.export_path = export_path
        self.cwd = cwd
        self.emit = emit
        self.init_emitted = False
        self.emitted_steps: set = set()
        self.emitted_observations: set = set()
        self.export: dict | None = None

    def poll(self) -> None:
        export = self._read_export()
        if export is None:
            return
        self.export = export
        if not self.init_emitted:
            self.emit(init_entry(export, self.cwd))
            self.init_emitted = True
        for step in export.get("steps") or []:
            step_id = step.get("step_id")
            if step_id not in self.emitted_steps:
                self.emitted_steps.add(step_id)
                for entry in step_to_entries(step):
                    self.emit(entry)
                if step.get("observation"):
                    self.emitted_observations.add(step_id)
            elif step.get("observation") and step_id not in self.emitted_observations:
                # Observation was filled in after the step was first exported
                self.emitted_observations.add(step_id)
                for entry in observation_entries(step):
                    self.emit(entry)

    def _read_export(self) -> dict | None:
        try:
            with open(self.export_path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict):
            return None
        return data


def run_devin_stream(
    command: list[str],
    export_path: str | None = None,
    poll_interval: float = POLL_INTERVAL,
    stdout=None,
) -> int:
    """Run a devin command, streaming its ATIF export as stream-json.

    Returns devin's exit code. ``command`` is the devin argv without
    ``--export``; it is injected pointing at ``export_path``.
    """
    out = stdout or sys.stdout
    if export_path is None:
        fd, export_path = tempfile.mkstemp(prefix="cacli-devin-", suffix=".json")
        os.close(fd)
        os.unlink(export_path)

    def emit(entry: dict) -> None:
        out.write(json.dumps(entry) + "\n")
        out.flush()

    argv = [command[0], "--export", export_path, *command[1:]]
    tailer = ExportTailer(export_path, os.getcwd(), emit)

    with tempfile.TemporaryFile(mode="w+", errors="replace") as devin_stdout:
        try:
            proc = subprocess.Popen(
                argv,
                stdout=devin_stdout,
                stderr=subprocess.DEVNULL,
                text=True,
            )
        except OSError as exc:
            emit(
                {
                    "type": "result",
                    "subtype": "error",
                    "is_error": True,
                    "result": f"failed to launch devin: {exc}",
                }
            )
            return 127

        while proc.poll() is None:
            tailer.poll()
            time.sleep(poll_interval)

        tailer.poll()
        devin_stdout.seek(0)
        final_stdout = devin_stdout.read().strip()

    emit(result_entry(tailer.export, final_stdout))
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cacli devin-stream",
        description="Run devin, streaming its trajectory as stream-json",
    )
    parser.add_argument("--export", dest="export_path", default=None)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    command = args.command
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        parser.error("missing devin command after --")

    return run_devin_stream(command, export_path=args.export_path)


if __name__ == "__main__":
    sys.exit(main())
