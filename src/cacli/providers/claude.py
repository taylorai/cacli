"""Claude Code provider."""

import json
import shlex

from cacli.providers.base import BaseProvider
from cacli.types import AgentRunResult


class ClaudeProvider(BaseProvider):
    """Provider for the Claude Code CLI."""

    name = "claude"

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        web_search: bool = True,
        settings: str | None = None,
    ) -> str:
        safe_prompt = shlex.quote(prompt)
        cmd = f"claude -p {safe_prompt} --verbose --output-format=stream-json"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        if reasoning_effort is not None:
            # Normalize unified effort levels to Claude Code values
            effort_map = {"max": "high", "xhigh": "high"}
            effort = effort_map.get(reasoning_effort, reasoning_effort)
            cmd += f" --effort {shlex.quote(effort)}"
        if settings:
            cmd += f" --settings {shlex.quote(settings)}"
        if not web_search:
            cmd += " --disallowedTools WebFetch --disallowedTools WebSearch"
        return cmd

    def parse_output(self, raw_output: str) -> AgentRunResult:
        result_message = ""
        total_cost = None
        permission_denials = []

        for line in reversed(raw_output.strip().split("\n")):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                entries = entry if isinstance(entry, list) else [entry]
                for item in reversed(entries):
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "result":
                        result_message = item.get("result", "")
                        total_cost = item.get("total_cost_usd")
                        permission_denials = item.get("permission_denials", [])
                        break
                if result_message:
                    break
            except json.JSONDecodeError:
                continue

        return AgentRunResult(
            result_message=result_message,
            total_cost=total_cost,
            permission_denials=permission_denials,
        )

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
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
    ) -> str:
        safe_prompt = shlex.quote(prompt)
        cmd = f"claude -p {safe_prompt} --verbose --output-format json"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        if settings:
            cmd += f" --settings {shlex.quote(settings)}"
        return cmd

    def extract_result_from_json(self, raw_output: str) -> str:
        # First try single-object JSON with "result" key
        try:
            data = json.loads(raw_output)
            if isinstance(data, dict) and "result" in data:
                return data["result"]
            if isinstance(data, list):
                for item in reversed(data):
                    if isinstance(item, dict) and "result" in item:
                        return item["result"]
        except json.JSONDecodeError:
            pass

        # Try JSONL: extract result from stream-json format
        # (same logic as parse_output)
        for line in reversed(raw_output.strip().split("\n")):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                entries = entry if isinstance(entry, list) else [entry]
                for item in reversed(entries):
                    if not isinstance(item, dict):
                        continue
                    if item.get("type") == "result":
                        return item.get("result", "")
            except json.JSONDecodeError:
                continue

        # Fallback: look for any line with "result" key
        for line in raw_output.split("\n"):
            line = line.strip()
            if line.startswith("{") and "result" in line:
                try:
                    data = json.loads(line)
                    if isinstance(data, dict) and "result" in data:
                        return data["result"]
                except json.JSONDecodeError:
                    continue

        return raw_output
