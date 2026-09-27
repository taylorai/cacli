"""OpenCode coding-agent harness."""

import json
import shlex
import warnings
from pathlib import Path
from typing import Any, Sequence

from cacli.harnesses.base import BaseHarness, Pairing
from cacli.types import AgentRunResult


def _numeric_cost(part: object) -> float | None:
    """Extract a numeric OpenCode step cost without accepting booleans."""
    if not isinstance(part, dict):
        return None
    cost = part.get("cost")
    if isinstance(cost, bool) or not isinstance(cost, (int, float)):
        return None
    return float(cost)


def _error_message(error: object) -> str:
    """Extract the most useful message from an OpenCode error event."""
    if isinstance(error, str):
        return error.strip()
    if not isinstance(error, dict):
        return ""
    data = error.get("data")
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = error.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    name = error.get("name")
    return name.strip() if isinstance(name, str) else ""


class OpenCodeHarness(BaseHarness):
    """Harness for the OpenCode CLI."""

    name = "opencode"
    pairings = (
        Pairing(
            "workers-ai",
            credentials=("CLOUDFLARE_API_KEY",),
            env=(("CLOUDFLARE_ACCOUNT_ID", "{account_id}"),),
            model_prefix="cloudflare-workers-ai",
            default_model="@cf/zai-org/glm-5.3",
        ),
        Pairing(
            "claude-api", credentials=("ANTHROPIC_API_KEY",), model_prefix="anthropic"
        ),
        Pairing("openai-api", credentials=("OPENAI_API_KEY",), model_prefix="openai"),
    )

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
        if not web_search:
            warnings.warn(
                "OpenCode does not support cacli's web_search option; "
                "web_search=False (--no-web-search) is ignored. "
                "Search availability is controlled by OpenCode configuration.",
                UserWarning,
                stacklevel=2,
            )
        if sandbox:
            raise NotImplementedError(
                "harness 'opencode' relies on external sandboxing and cannot "
                "enable an OS sandbox itself"
            )

        parts = [
            *exec_prefix,
            "opencode",
            "run",
            "--format",
            "json",
            "--auto",
        ]
        if model:
            parts.extend(["--model", model])
        if reasoning_effort and reasoning_effort != "auto":
            parts.extend(["--variant", reasoning_effort])

        if settings is not None:
            if not exec_prefix:
                parts = ["env", f"OPENCODE_CONFIG={settings}", *parts]
            else:
                parts.insert(len(exec_prefix), f"OPENCODE_CONFIG={settings}")
        # Stdin avoids CLI flag parsing and OpenCode's positional quote rewriting.
        return f"printf '%s' {shlex.quote(prompt)} | {shlex.join(parts)}"

    def parse_output(self, raw_output: str) -> AgentRunResult:
        texts_by_message: dict[str, list[str]] = {}
        message_order: list[str] = []
        seen_text_parts: set[str] = set()
        seen_finish_parts: set[str] = set()
        total_cost = 0.0
        saw_cost = False
        last_error = ""
        anonymous_index = 0

        for line in raw_output.splitlines():
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue

            event_type = event.get("type")
            if event_type == "error":
                message = _error_message(event.get("error"))
                last_error = message or "OpenCode harness returned an error"
                continue

            part = event.get("part")
            if event_type == "text" and isinstance(part, dict):
                text = part.get("text")
                if not isinstance(text, str) or not text.strip():
                    continue
                part_id = part.get("id")
                if isinstance(part_id, str):
                    if part_id in seen_text_parts:
                        continue
                    seen_text_parts.add(part_id)
                message_id = part.get("messageID")
                if not isinstance(message_id, str):
                    anonymous_index += 1
                    message_id = f"anonymous-{anonymous_index}"
                if message_id not in texts_by_message:
                    texts_by_message[message_id] = []
                    message_order.append(message_id)
                texts_by_message[message_id].append(text.strip())
                continue

            if event_type == "step_finish" and isinstance(part, dict):
                part_id = part.get("id")
                if isinstance(part_id, str):
                    if part_id in seen_finish_parts:
                        continue
                    seen_finish_parts.add(part_id)
                cost = _numeric_cost(part)
                if cost is not None:
                    total_cost += cost
                    saw_cost = True

        result_message = ""
        if last_error:
            result_message = last_error
        elif message_order:
            result_message = "\n".join(texts_by_message[message_order[-1]]).strip()

        return AgentRunResult(
            exit_code=1 if last_error else 0,
            result_message=result_message,
            total_cost=total_cost if saw_cost else None,
        )

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        entry: dict[str, Any] = {
            "type": "user",
            "harness": "opencode",
            "message": {"role": "user", "content": prompt},
        }
        if model:
            entry["model"] = model
        return json.dumps(entry)

    def build_pr_description_command(
        self,
        prompt: str,
        model: str | None = None,
        settings: str | None = None,
        sandbox: bool | None = None,
        exec_prefix: Sequence[str] = (),
    ) -> str:
        return self.build_command(
            prompt=prompt,
            model=model,
            settings=settings,
            sandbox=sandbox,
            exec_prefix=exec_prefix,
        )

    def extract_result_from_json(self, raw_output: str) -> str:
        return self.parse_output(raw_output).result_message or raw_output

    def skills_dir(self) -> str:
        return "/root/.config/opencode/skills"
