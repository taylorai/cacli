"""Cursor harness."""

import json
import shlex
from pathlib import Path
from typing import Sequence

from cacli.harnesses.base import BaseHarness, Pairing
from cacli.types import AgentRunResult


class CursorHarness(BaseHarness):
    """Harness for Cursor Agent CLI."""

    name = "cursor"
    pairings = (
        Pairing(
            "cursor", credentials=("CURSOR_API_KEY",), default_model="composer-2.5"
        ),
    )

    def resolve_model(self, model: str | None) -> str:
        if not model:
            default = self.pairings[0].default_model
            assert default is not None
            return default
        return model

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
        resolved_model = self.resolve_model(model)
        safe_prompt = shlex.quote(prompt)
        cmd = f"{self.command_head('agent', exec_prefix)} --print --output-format stream-json --force --model {shlex.quote(resolved_model)}"
        if sandbox is not None:
            cmd += f" --sandbox {'enabled' if sandbox else 'disabled'}"
        cmd += f" {safe_prompt}"
        return cmd

    def parse_output(self, raw_output: str) -> AgentRunResult:
        result_message = ""

        for line in reversed(raw_output.strip().split("\n")):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    message = entry.get("message", {})
                    content = message.get("content", [])
                    if isinstance(content, list) and content:
                        text = content[0].get("text")
                        if text:
                            result_message = text
                            break
            except json.JSONDecodeError:
                continue

        return AgentRunResult(result_message=result_message)

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        # Cursor uses the same format as Claude for initial log entries
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
        resolved_model = self.resolve_model(model)
        safe_prompt = shlex.quote(prompt)
        cmd = f"{self.command_head('agent', exec_prefix)} --print --output-format json --force --model {shlex.quote(resolved_model)}"
        if sandbox is not None:
            cmd += f" --sandbox {'enabled' if sandbox else 'disabled'}"
        cmd += f" {safe_prompt}"
        return cmd

    def extract_result_from_json(self, raw_output: str) -> str:
        # First try single-object JSON with "result" key
        try:
            data = json.loads(raw_output)
            if isinstance(data, dict) and "result" in data:
                return data["result"]
        except json.JSONDecodeError:
            pass

        # Try JSONL: extract last assistant message from stream-json format
        # (same logic as parse_output)
        for line in reversed(raw_output.strip().split("\n")):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "assistant":
                    message = entry.get("message", {})
                    content = message.get("content", [])
                    if isinstance(content, list) and content:
                        text = content[0].get("text")
                        if text:
                            return text
            except json.JSONDecodeError:
                continue

        # Fallback: look for any line with "result" key
        for line in raw_output.split("\n"):
            line = line.strip()
            if line.startswith("{") and "result" in line:
                try:
                    data = json.loads(line)
                    if "result" in data:
                        return data["result"]
                except json.JSONDecodeError:
                    continue

        return raw_output

    def skills_dir(self) -> str:
        return "/root/.cursor/skills"
