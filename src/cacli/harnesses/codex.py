"""Codex/OpenAI harness."""

import json
import shlex
from pathlib import Path
from typing import Sequence

from cacli.harnesses.base import BaseHarness, Pairing
from cacli.inference import PROVIDERS
from cacli.types import AgentRunResult

# workspace-write confines writes to the workspace at the OS level;
# --no-sandbox falls back to danger-full-access (the old behavior, for
# externally-sandboxed environments).
CODEX_DEFAULT_SANDBOX = True


def _sandbox_args(sandbox: bool | None) -> str:
    if sandbox is None:
        sandbox = CODEX_DEFAULT_SANDBOX
    if not sandbox:
        return "--sandbox danger-full-access"
    # Codex blocks network inside workspace-write by default; allow it so
    # sandboxed runs match Devin's sandbox semantics (FS confined, net open).
    return (
        "--sandbox workspace-write --config sandbox_workspace_write.network_access=true"
    )


class CodexHarness(BaseHarness):
    """Harness for Codex/OpenAI CLI."""

    name = "codex"
    pairings = (
        Pairing(
            "openai-sub",
            credentials=("~/.codex/auth.json",),
            default_model="gpt-5.4",
            default_reasoning_effort="high",
            unset_env=("OPENAI_API_KEY", "CODEX_API_KEY"),
        ),
        Pairing(
            "openai-api",
            credentials=("OPENAI_API_KEY",),
            default_model="gpt-5.4",
            default_reasoning_effort="high",
        ),
        Pairing(
            "bedrock",
            credentials=("AWS_BEARER_TOKEN_BEDROCK",),
            requires_model=True,
            verified=False,
        ),
        Pairing(
            "workers-ai",
            credentials=("CLOUDFLARE_API_KEY",),
            requires_model=True,
            verified=False,
        ),
    )

    def configure_command(
        self, pair: Pairing, command: str, params: dict[str, str]
    ) -> str:
        catalog = PROVIDERS[pair.provider]
        if catalog.openai_base_url is None:
            return command
        ident = pair.provider.replace("-", "_")
        env_key = catalog.credentials[0].env
        values = {
            "model_provider": ident,
            f"model_providers.{ident}.name": pair.provider,
            f"model_providers.{ident}.base_url": catalog.openai_base_url.format(
                **params
            ),
            f"model_providers.{ident}.env_key": env_key,
            f"model_providers.{ident}.wire_api": "responses",
        }
        return command + "".join(
            f" --config {shlex.quote(f'{key}={value}')}"
            for key, value in values.items()
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
        safe_prompt = shlex.quote(prompt)
        cmd = (
            f"{self.command_head('codex', exec_prefix)} exec {safe_prompt} {_sandbox_args(sandbox)}"
            " --json --skip-git-repo-check"
        )
        if model is not None:
            cmd += f" --model {shlex.quote(model)}"
        if reasoning_effort is not None:
            cmd += f" --config model_reasoning_effort={shlex.quote(reasoning_effort)}"

        if web_search:
            cmd += " --config features.web_search_request=true"
        if output_schema is not None:
            cmd += f" --output-schema {shlex.quote(str(output_schema))}"
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
        sandbox: bool | None = None,
        exec_prefix: Sequence[str] = (),
    ) -> str:
        safe_prompt = shlex.quote(prompt)
        cmd = (
            f"{self.command_head('codex', exec_prefix)} exec {safe_prompt} {_sandbox_args(sandbox)}"
            " --json --skip-git-repo-check"
        )
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
