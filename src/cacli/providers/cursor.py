"""Cursor provider."""

import json
import shlex

from cacli.providers.base import BaseProvider
from cacli.types import AgentRunResult

CURSOR_DEFAULT_MODEL = "composer-2"

CURSOR_ALLOWED_MODELS = {
    "auto",  # - Auto  (current)
    "composer-2-fast",  #  - Composer 2 Fast
    "composer-2",  # - Composer 2
    "composer-1.5",  # - Composer 1.5
    "gpt-5.3-codex-low",  # - GPT-5.3 Codex Low
    "gpt-5.3-codex-low-fast",  # - GPT-5.3 Codex Low Fast
    "gpt-5.3-codex",  # - GPT-5.3 Codex
    "gpt-5.3-codex-fast",  # - GPT-5.3 Codex Fast
    "gpt-5.3-codex-high",  # - GPT-5.3 Codex High
    "gpt-5.3-codex-high-fast",  # - GPT-5.3 Codex High Fast
    "gpt-5.3-codex-xhigh",  # - GPT-5.3 Codex Extra High
    "gpt-5.3-codex-xhigh-fast",  # - GPT-5.3 Codex Extra High Fast
    "gpt-5.2",  # - GPT-5.2
    "gpt-5.3-codex-spark-preview",  # - GPT-5.3 Codex Spark
    "gpt-5.2-codex-low",  # - GPT-5.2 Codex Low
    "gpt-5.2-codex-low-fast",  # - GPT-5.2 Codex Low Fast
    "gpt-5.2-codex",  # - GPT-5.2 Codex
    "gpt-5.2-codex-fast",  # - GPT-5.2 Codex Fast
    "gpt-5.2-codex-high",  # - GPT-5.2 Codex High
    "gpt-5.2-codex-high-fast",  # - GPT-5.2 Codex High Fast
    "gpt-5.2-codex-xhigh",  # - GPT-5.2 Codex Extra High
    "gpt-5.2-codex-xhigh-fast",  # - GPT-5.2 Codex Extra High Fast
    "gpt-5.1-codex-max-high",  # - GPT-5.1 Codex Max High
    "gpt-5.4-high",  # - GPT-5.4 High
    "gpt-5.4-high-fast",  # - GPT-5.4 High Fast
    "gpt-5.4-xhigh-fast",  # - GPT-5.4 Extra High Fast
    "opus-4.6-thinking",  # - Claude 4.6 Opus (Thinking)  (default)
    "gpt-5.4-low",  # - GPT-5.4 Low
    "gpt-5.4-medium",  # - GPT-5.4
    "gpt-5.4-medium-fast",  # - GPT-5.4 Fast
    "gpt-5.4-xhigh",  # - GPT-5.4 Extra High
    "sonnet-4.6",  # - Claude 4.6 Sonnet
    "sonnet-4.6-thinking",  # - Claude 4.6 Sonnet (Thinking)
    "opus-4.6",  # - Claude 4.6 Opus
    "opus-4.5",  # - Claude 4.5 Opus
    "opus-4.5-thinking",  # - Claude 4.5 Opus (Thinking)
    "gpt-5.2-high",  # - GPT-5.2 High
    "gemini-3.1-pro",  # - Gemini 3.1 Pro
    "sonnet-4.5",  # - Claude 4.5 Sonnet
    "sonnet-4.5-thinking",  # - Claude 4.5 Sonnet (Thinking)
    "gpt-5.1-low",  # - GPT-5.1 Low
    "gpt-5.1",  # - GPT-5.1
    "gpt-5.1-high",  # - GPT-5.1 High
    "gemini-3-pro",  # - Gemini 3 Pro
    "gemini-3-flash",  # - Gemini 3 Flash
    "gpt-5.1-codex-mini",  # - GPT-5.1 Codex Mini
    "kimi-k2.5",  # - Kimi K2.5
}


class CursorProvider(BaseProvider):
    """Provider for Cursor Agent CLI."""

    name = "cursor"

    def resolve_model(self, model: str | None) -> str:
        if not model:
            return CURSOR_DEFAULT_MODEL
        if model not in CURSOR_ALLOWED_MODELS:
            allowed = ", ".join(sorted(CURSOR_ALLOWED_MODELS))
            raise ValueError(f"Unsupported cursor model '{model}'. Allowed: {allowed}")
        return model

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        web_search: bool = True,
        settings: str | None = None,
    ) -> str:
        resolved_model = self.resolve_model(model)
        safe_prompt = shlex.quote(prompt)
        cmd = f"agent --print --output-format stream-json --force --model {shlex.quote(resolved_model)} {safe_prompt}"
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
    ) -> str:
        resolved_model = self.resolve_model(model)
        safe_prompt = shlex.quote(prompt)
        cmd = f"agent --print --output-format json --force --model {shlex.quote(resolved_model)} {safe_prompt}"
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
