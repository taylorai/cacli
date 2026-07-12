"""Validated host callbacks exposed to sandboxed workflows."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from cacli.flow.engine import FlowEngine
from cacli.providers import list_providers


EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra", "auto"}


def default_concurrency() -> int:
    return min(16, max(2, (os.cpu_count() or 2) - 2))


def _optional_string(name: str, value: Any) -> str | None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{name} must be a string or null")
    return value


def _positive_number(name: str, value: Any, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number")
    if value <= 0 or value > maximum:
        raise ValueError(f"{name} must be > 0 and <= {maximum:g}")
    return float(value)


class FlowHost:
    """The capability boundary between Monty and real provider subprocesses."""

    def __init__(
        self,
        engine: FlowEngine,
        *,
        cwd: Path,
        default_provider: str = "claude",
    ):
        self.engine = engine
        self.cwd = cwd.resolve()
        self.default_provider = default_provider
        self.ambient_phase: str | None = None

    def _ctx_phase(self, title: Any) -> None:
        if not isinstance(title, str) or not title.strip():
            raise ValueError("phase title must be a non-empty string")
        if len(title) > 200:
            raise ValueError("phase title is too long (max 200 characters)")
        self.ambient_phase = title
        self.engine._event("phase", title=title)

    def _ctx_log(self, message: Any) -> None:
        if not isinstance(message, str):
            raise TypeError("log message must be a string")
        if len(message) > 10_000:
            raise ValueError("log message is too long (max 10000 characters)")
        self.engine._event("log", message=message, phase=self.ambient_phase)

    def _validate_cwd(self, cwd: Any) -> tuple[str, str | None]:
        if cwd is None:
            return str(self.cwd), None
        if not isinstance(cwd, str):
            raise TypeError("cwd must be a string or null")
        candidate = (
            (self.cwd / cwd).resolve()
            if not Path(cwd).is_absolute()
            else Path(cwd).resolve()
        )
        try:
            candidate.relative_to(self.cwd)
        except ValueError as exc:
            raise ValueError(
                "cwd must be the flow cwd or a directory beneath it"
            ) from exc
        if not candidate.is_dir():
            raise ValueError(f"cwd is not a directory: {cwd}")
        relative = candidate.relative_to(self.cwd)
        key_cwd = None if relative == Path(".") else relative.as_posix()
        return str(candidate), key_cwd

    def _ctx_budget_total(self) -> float | None:
        return self.engine.budget

    def _ctx_budget_spent(self) -> float:
        return self.engine.spent

    def _ctx_budget_remaining(self) -> float:
        if self.engine.budget is None:
            return float("inf")
        return max(0.0, self.engine.budget - self.engine.spent)

    async def _ctx_agent(
        self,
        prompt: Any,
        *,
        provider: Any = None,
        model: Any = None,
        effort: Any = None,
        schema: Any = None,
        label: Any = None,
        phase: Any = None,
        cwd: Any = None,
        isolation: Any = None,
        timeout: Any = 3600,
        stall_timeout: Any = 300,
        retries: Any = 2,
        web_search: Any = True,
        settings: Any = None,
        sandbox: Any = None,
        env: Any = None,
    ) -> str | None:
        if self.engine.cancel_event.is_set():
            raise RuntimeError("FlowCancelled: workflow cancelled")
        if not isinstance(prompt, str):
            raise TypeError("prompt must be a string")
        if len(prompt) > 1_000_000:
            raise ValueError("prompt is too long (max 1000000 characters)")
        provider_name = self.default_provider if provider is None else provider
        if not isinstance(provider_name, str) or provider_name not in list_providers():
            raise ValueError(
                f"provider must be a registered provider name: {', '.join(list_providers())}"
            )
        model = _optional_string("model", model)
        if model is not None and len(model) > 200:
            raise ValueError("model is too long (max 200 characters)")
        if effort is not None and (
            not isinstance(effort, str) or effort not in EFFORTS
        ):
            raise ValueError(f"effort must be one of: {', '.join(sorted(EFFORTS))}")
        if schema is not None:
            raise ValueError("schema support lands in Phase 3")
        label = _optional_string("label", label)
        phase = _optional_string("phase", phase)
        settings = _optional_string("settings", settings)
        if settings is not None and len(settings) > 4096:
            raise ValueError("settings is too long (max 4096 characters)")
        if isolation is not None:
            raise ValueError("worktree isolation support lands in Phase 4")
        timeout_value = _positive_number("timeout", timeout, 86_400)
        stall_value = _positive_number("stall_timeout", stall_timeout, 86_400)
        if (
            isinstance(retries, bool)
            or not isinstance(retries, int)
            or not 0 <= retries <= 10
        ):
            raise ValueError("retries must be an integer from 0 to 10")
        if not isinstance(web_search, bool):
            raise TypeError("web_search must be a boolean")
        if sandbox is not None and not isinstance(sandbox, bool):
            raise TypeError("sandbox must be a boolean or null")
        if env is not None:
            if not isinstance(env, dict) or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in env.items()
            ):
                raise TypeError("env must be a dict of string keys and values")
            if len(env) > 100:
                raise ValueError("env has too many entries (max 100)")
            if any(
                len(key) > 200 or len(value) > 100_000 for key, value in env.items()
            ):
                raise ValueError("env key or value is too long")
        if label is None:
            label = re.sub(r"\s+", " ", prompt).strip()[:60] or "agent"
        if len(label) > 200:
            raise ValueError("label is too long (max 200 characters)")
        selected_phase = phase if phase is not None else self.ambient_phase
        absolute_cwd, cwd_key = self._validate_cwd(cwd)
        return await self.engine.agent(
            prompt,
            provider_name=provider_name,
            model=model,
            effort=effort,
            label=label,
            phase=selected_phase,
            cwd=absolute_cwd,
            cwd_key=cwd_key,
            schema=schema,
            isolation=isolation,
            timeout=timeout_value,
            stall_timeout=stall_value,
            retries=retries,
            web_search=web_search,
            settings=settings,
            sandbox=sandbox,
            env=env,
        )
