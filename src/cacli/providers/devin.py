"""Devin CLI provider."""

import json
import shlex
from typing import Any

from cacli.providers.base import BaseProvider
from cacli.types import AgentRunResult

DEVIN_DEFAULT_PERMISSION_MODE = "dangerous"


class DevinProvider(BaseProvider):
    """Provider for the Devin CLI."""

    name = "devin"

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        web_search: bool = True,
        settings: str | None = None,
    ) -> str:
        safe_prompt = shlex.quote(prompt)
        cmd = f"devin --permission-mode {DEVIN_DEFAULT_PERMISSION_MODE}"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        if settings:
            cmd += f" --config {shlex.quote(settings)}"
        cmd += f" --print {safe_prompt}"
        return cmd

    def parse_output(self, raw_output: str) -> AgentRunResult:
        return AgentRunResult(result_message=self.extract_result_from_json(raw_output))

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        entry = {
            "type": "user",
            "provider": "devin",
            "message": {
                "role": "user",
                "content": prompt,
            },
        }
        if model:
            entry["model"] = model
        return json.dumps(entry)

    def build_pr_description_command(
        self,
        prompt: str,
        model: str | None = None,
        settings: str | None = None,
    ) -> str:
        return self.build_command(prompt=prompt, model=model, settings=settings)

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
