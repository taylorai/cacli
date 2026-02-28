"""Codex/OpenAI provider."""

import json
import shlex

from cacli.providers.base import BaseProvider
from cacli.types import AgentRunResult


class CodexProvider(BaseProvider):
    """Provider for Codex/OpenAI CLI."""

    name = "codex"

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        web_search: bool = True,
        settings: str | None = None,
    ) -> str:
        safe_prompt = shlex.quote(prompt)
        cmd = f"codex exec {safe_prompt} --sandbox danger-full-access --json --skip-git-repo-check"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        if reasoning_effort is not None:
            cmd += f" --config model_reasoning_effort={reasoning_effort}"
        if web_search:
            cmd += " --config features.web_search_request=true"
        return cmd

    def parse_output(self, raw_output: str) -> AgentRunResult:
        result_message = ""

        for line in reversed(raw_output.strip().split("\n")):
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "item.completed":
                    item = entry.get("item", {})
                    if item.get("type") == "agent_message":
                        result_message = item.get("text", "")
                        break
            except json.JSONDecodeError:
                continue

        return AgentRunResult(result_message=result_message)

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        item = {
            "id": "item_user",
            "type": "user_message",
            "text": prompt,
        }
        if model:
            item["model"] = model
        return json.dumps({"type": "item.completed", "item": item})

    def build_pr_description_command(
        self,
        prompt: str,
        model: str | None = None,
        settings: str | None = None,
    ) -> str:
        safe_prompt = shlex.quote(prompt)
        cmd = f"codex exec {safe_prompt} --sandbox danger-full-access --json --skip-git-repo-check"
        if model:
            cmd += f" --model {shlex.quote(model)}"
        return cmd

    def extract_result_from_json(self, raw_output: str) -> str:
        # First try single-object JSON with "result" key
        try:
            data = json.loads(raw_output)
            if isinstance(data, dict) and "result" in data:
                return data["result"]
        except json.JSONDecodeError:
            pass

        # Try JSONL: extract last agent_message from item.completed events
        # (same logic as parse_output)
        for line in reversed(raw_output.strip().split("\n")):
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                if entry.get("type") == "item.completed":
                    item = entry.get("item", {})
                    if item.get("type") == "agent_message":
                        return item.get("text", "")
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
        return "/root/.codex/skills"
