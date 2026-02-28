"""Gemini provider."""

import json
import shlex

from cacli.providers.base import BaseProvider
from cacli.types import AgentRunResult

GEMINI_MODEL_ALIASES = {
    "3-pro": "gemini-3-pro-preview",
    "pro-3": "gemini-3-pro-preview",
    "3-flash": "gemini-3-flash-preview",
    "flash-3": "gemini-3-flash-preview",
    "2.5-pro": "gemini-2.5-pro",
    "pro": "gemini-2.5-pro",
    "2.5-flash": "gemini-2.5-flash",
    "flash": "gemini-2.5-flash",
    "flash-lite": "gemini-2.5-flash-lite",
    "lite": "gemini-2.5-flash-lite",
}

GEMINI_DEFAULT_MODEL = "gemini-3-flash-preview"


class GeminiProvider(BaseProvider):
    """Provider for Google Gemini CLI."""

    name = "gemini"

    def resolve_model(self, model: str | None) -> str:
        if model:
            return GEMINI_MODEL_ALIASES.get(model, model)
        return GEMINI_DEFAULT_MODEL

    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        web_search: bool = True,
        settings: str | None = None,
    ) -> str:
        safe_prompt = shlex.quote(prompt)
        resolved_model = self.resolve_model(model)
        cmd = f"gemini -p {safe_prompt} -y --output-format stream-json -m {shlex.quote(resolved_model)} 2>&1 | grep '^{{'"
        return cmd

    def parse_output(self, raw_output: str) -> AgentRunResult:
        # Gemini stream-json emits typed events. Parse assistant messages and
        # explicitly skip unknown orchestrator event lines.
        assistant_messages: list[str] = []
        fallback_lines: list[str] = []

        for line in raw_output.strip().split("\n"):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entry = json.loads(stripped)
            except json.JSONDecodeError:
                fallback_lines.append(stripped)
                continue

            if entry.get("type") == "ccremote.event":
                continue

            event_type = entry.get("type")
            if event_type == "message" and entry.get("role") == "assistant":
                content = entry.get("content")
                if isinstance(content, str) and content.strip():
                    assistant_messages.append(content.strip())
                continue

            # Keep non-event JSON lines as a fallback text trace.
            fallback_lines.append(stripped)

        if assistant_messages:
            return AgentRunResult(result_message=assistant_messages[-1])

        return AgentRunResult(result_message="\n".join(fallback_lines).strip())

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        entry = {
            "type": "user",
            "provider": "gemini",
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
        safe_prompt = shlex.quote(prompt)
        resolved_model = self.resolve_model(model)
        cmd = f"gemini -p {safe_prompt} -y -m {shlex.quote(resolved_model)}"
        return cmd

    def extract_result_from_json(self, raw_output: str) -> str:
        # Gemini plain text output — return as-is
        return raw_output

    def skills_dir(self) -> str:
        return "/root/.gemini/skills"
