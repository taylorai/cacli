"""Pi coding-agent harness."""

import json
import shlex
from pathlib import Path
from typing import Any, Sequence

from cacli.harnesses.base import BaseHarness, Pairing
from cacli.types import AgentRunResult


def _assistant_text(message: object) -> str:
    """Extract visible text blocks from a Pi assistant message."""
    if not isinstance(message, dict) or message.get("role") != "assistant":
        return ""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    blocks = [
        item.get("text", "")
        for item in content
        if isinstance(item, dict)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    return "\n".join(block for block in blocks if block).strip()


def _message_cost(message: object) -> float | None:
    """Extract the per-message cost reported by Pi, if present."""
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None
    cost = usage.get("cost")
    if not isinstance(cost, dict):
        return None
    total = cost.get("total")
    if isinstance(total, bool) or not isinstance(total, (int, float)):
        return None
    return float(total)


class PiHarness(BaseHarness):
    """Harness for the Pi coding-agent CLI."""

    name = "pi"
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
        extensions: Sequence[str] = (),
        trust_project: bool = True,
    ) -> str:
        if sandbox:
            raise NotImplementedError(
                "harness 'pi' relies on external sandboxing and cannot enable "
                "an OS sandbox itself"
            )
        if settings is not None:
            raise ValueError("harness 'pi' does not support a settings argument")

        parts = [
            *exec_prefix,
            "pi",
            "--mode",
            "json",
            "--print",
            "--no-session",
        ]
        # cacli runs Pi in externally isolated environments, so by default it
        # trusts the project and loads repository-local settings and resources.
        parts.append("--approve" if trust_project else "--no-approve")
        for extension in extensions:
            parts.extend(["-e", extension])
        if model:
            parts.extend(["--model", model])
        if reasoning_effort and reasoning_effort != "auto":
            parts.extend(["--thinking", reasoning_effort])
        # Stdin keeps leading '-' and '@' in prompts out of Pi's CLI parser.
        return f"printf '%s' {shlex.quote(prompt)} | {shlex.join(parts)}"

    def parse_output(self, raw_output: str) -> AgentRunResult:
        result_message = ""
        total_cost = 0.0
        saw_cost = False
        provider_error: str | None = None

        for line in raw_output.splitlines():
            try:
                event: Any = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("type") != "message_end":
                continue

            message = event.get("message")
            text = _assistant_text(message)
            if text:
                result_message = text

            if isinstance(message, dict) and message.get("role") == "assistant":
                stop_reason = message.get("stopReason")
                if stop_reason in ("error", "aborted"):
                    error_message = message.get("errorMessage")
                    provider_error = (
                        error_message
                        if isinstance(error_message, str) and error_message
                        else f"Pi request ended with {stop_reason}"
                    )
                else:
                    # Pi may emit a retryable error before a later successful
                    # assistant message. Only the final assistant state wins.
                    provider_error = None

            cost = _message_cost(message)
            if cost is not None:
                total_cost += cost
                saw_cost = True

        return AgentRunResult(
            exit_code=1 if provider_error else 0,
            result_message=provider_error or result_message,
            total_cost=total_cost if saw_cost else None,
        )

    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        entry: dict[str, Any] = {
            "type": "user",
            "harness": "pi",
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
        extensions: Sequence[str] = (),
        trust_project: bool = True,
    ) -> str:
        return self.build_command(
            prompt=prompt,
            model=model,
            settings=settings,
            sandbox=sandbox,
            exec_prefix=exec_prefix,
            extensions=extensions,
            trust_project=trust_project,
        )

    def extract_result_from_json(self, raw_output: str) -> str:
        return self.parse_output(raw_output).result_message or raw_output

    def skills_dir(self) -> str:
        return "/root/.pi/agent/skills"
