"""Devin CLI harness."""

import json
import shlex
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Sequence

from cacli.harnesses.base import BaseHarness, Pairing
from cacli.types import AgentRunResult

DEVIN_DEFAULT_PERMISSION_MODE = "dangerous"
# OS-level isolation (macOS seatbelt / Linux bwrap+seccomp). Devin is
# fail-closed: with --sandbox it refuses to start if the platform can't
# sandbox (Windows, or Linux without bubblewrap+socat) — pass
# sandbox=False / --no-sandbox in such environments.
DEVIN_DEFAULT_SANDBOX = True


def _stream_wrapper_prefix() -> str:
    """Command prefix for the devin-stream wrapper.

    Prefer the installed console script; fall back to running the module
    with the current interpreter (e.g. PYTHONPATH=src development runs).
    """
    if shutil.which("cacli"):
        return "cacli devin-stream"
    return f"{shlex.quote(sys.executable)} -m cacli.devin_stream"


class DevinHarness(BaseHarness):
    """Harness for the Devin CLI."""

    name = "devin"
    pairings = (
        Pairing(
            "devin",
            credentials=("~/.local/share/devin/credentials.toml",),
            verified=False,
        ),
    )

    def build_devin_command(
        self,
        prompt: str,
        model: str | None = None,
        settings: str | None = None,
        sandbox: bool | None = None,
        exec_prefix: Sequence[str] = (),
    ) -> str:
        """Build the bare devin invocation (no trajectory streaming).

        --permission-mode dangerous is always required in --print mode
        (there is no one to answer approval prompts); --sandbox adds
        OS-level confinement of writes to the workspace on top of it.
        """
        if sandbox is None:
            sandbox = DEVIN_DEFAULT_SANDBOX
        safe_prompt = shlex.quote(prompt)
        cmd = self.command_head("devin", exec_prefix)
        if sandbox:
            cmd += " --sandbox"
        cmd += f" --permission-mode {DEVIN_DEFAULT_PERMISSION_MODE}"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        if settings:
            cmd += f" --config {shlex.quote(settings)}"
        cmd += f" --print {safe_prompt}"
        return cmd

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        web_search: bool = True,
        settings: str | None = None,
        sandbox: bool | None = None,
        output_schema: dict | Path | None = None,
        exec_prefix: Sequence[str] = (),
    ) -> str:
        # Devin's --print mode only writes the final answer to stdout, so the
        # command is wrapped with the devin-stream converter, which tails
        # Devin's ATIF --export file and emits Claude-style stream-json.
        devin_cmd = self.build_devin_command(
            prompt,
            model=model,
            settings=settings,
            sandbox=sandbox,
            exec_prefix=exec_prefix,
        )
        export_path = str(
            Path(tempfile.gettempdir()) / f"cacli-devin-{uuid.uuid4().hex[:12]}.json"
        )
        return (
            f"{_stream_wrapper_prefix()} --export {shlex.quote(export_path)}"
            f" -- {devin_cmd}"
        )

    def parse_output(self, raw_output: str) -> AgentRunResult:
        return AgentRunResult(result_message=self.extract_result_from_json(raw_output))

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        # The devin-stream wrapper emits Claude-style stream-json, so the
        # initial entry uses the same format as the Claude harness.
        return json.dumps(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "text", "text": prompt}],
                },
            }
        )

    def build_pr_description_command(
        self,
        prompt: str,
        model: str | None = None,
        settings: str | None = None,
        sandbox: bool | None = None,
        exec_prefix: Sequence[str] = (),
    ) -> str:
        # Only the final text matters here, so skip the streaming wrapper.
        return self.build_devin_command(
            prompt,
            model=model,
            settings=settings,
            sandbox=sandbox,
            exec_prefix=exec_prefix,
        )

    def extract_result_from_json(self, raw_output: str) -> str:
        stripped = raw_output.strip()
        if not stripped:
            return ""

        json_result = _extract_text_from_jsonish(stripped)
        if json_result:
            return json_result

        return stripped


def _extract_text_from_jsonish(raw_output: str) -> str:
    try:
        data = json.loads(raw_output)
    except json.JSONDecodeError:
        return _extract_text_from_json_lines(raw_output)

    return _extract_text(data)


def _extract_text_from_json_lines(raw_output: str) -> str:
    for line in reversed(raw_output.splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        text = _extract_text(data)
        if text:
            return text
    return ""


def _extract_text(data: Any) -> str:
    if isinstance(data, str):
        return data

    if isinstance(data, list):
        for item in reversed(data):
            text = _extract_text(item)
            if text:
                return text
        return ""

    if not isinstance(data, dict):
        return ""

    for key in ("result", "result_message", "text", "content", "message"):
        value = data.get(key)
        if isinstance(value, str):
            return value
        if isinstance(value, list | dict):
            text = _extract_text(value)
            if text:
                return text

    return ""
