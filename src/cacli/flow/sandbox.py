"""Monty 0.0.18 compatibility seam for workflow scripts.

All pydantic-monty imports and API calls intentionally live in this module so
the eventual crash-isolated 0.0.19 migration does not leak across the engine.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Any, cast

from pydantic_monty import Monty, ResourceLimits

from cacli.flow.shim import EPILOGUE, PRELUDE


# On monty 0.0.18 the max_duration_secs clock pauses during a SINGLE pending
# host await but keeps running while suspended on multiple concurrent futures
# (asyncio.gather fan-outs) — so it effectively measures wall clock for real
# flows and must exceed any plausible flow duration. 24h keeps a bound on true
# runaway loops; agent wall-clock is enforced host-side in engine/aexec.
# Revisit with 0.0.19's worker watchdog.
DEFAULT_LIMITS = ResourceLimits(
    max_duration_secs=86_400.0,
    max_memory=64 * 1024 * 1024,
    max_allocations=500_000,
    max_recursion_depth=200,
)


class FlowScriptError(RuntimeError):
    """A compile-time or runtime error from the sandboxed flow."""


def extract_meta(source: str, script_name: str = "<flow>") -> dict[str, Any]:
    """Extract a literal top-level META assignment without executing source."""
    try:
        tree = ast.parse(source, filename=script_name)
    except SyntaxError as exc:
        raise FlowScriptError(str(exc)) from exc
    for node in tree.body:
        target = None
        value = None
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            target = node.target
            value = node.value
        if isinstance(target, ast.Name) and target.id == "META" and value is not None:
            try:
                meta = ast.literal_eval(value)
            except (ValueError, TypeError, SyntaxError) as exc:
                raise FlowScriptError("META must be a literal dict") from exc
            if not isinstance(meta, dict):
                raise FlowScriptError("META must be a literal dict")
            return meta
    return {}


def _adjust_display(display: str, user_lines: int, prelude_lines: int) -> str:
    """Translate combined-source line numbers back to the user script."""

    def replace(match: re.Match[str]) -> str:
        line = int(match.group(1))
        if prelude_lines < line <= prelude_lines + user_lines:
            return f"line {line - prelude_lines}"
        return match.group(0)

    return re.sub(r"line (\d+)", replace, display)


def _error_display(exc: Exception, user_lines: int, prelude_lines: int) -> str:
    display = getattr(exc, "display", None)
    if callable(display):
        try:
            return _adjust_display(display(), user_lines, prelude_lines)
        except Exception:
            pass
    return _adjust_display(str(exc), user_lines, prelude_lines)


async def execute(
    source: str,
    script_name: str,
    args: Any,
    host: Any,
    *,
    limits: dict[str, Any] | None = None,
) -> Any:
    """Compile and execute a user flow with only the host-provided capabilities."""
    combined = PRELUDE + source + EPILOGUE
    prelude_lines = PRELUDE.count("\n")
    user_lines = source.count("\n") + 1
    try:
        program = Monty(
            combined,
            inputs=["_ctx_args"],
            script_name=Path(script_name).name,
        )
        # max_duration_secs measures interpreter CPU time in 0.0.18 and pauses
        # during async host calls (conformance-tested); agent wall time remains
        # bounded by engine/aexec timeouts.
        return await program.run_async(
            inputs={"_ctx_args": args},
            external_functions={
                "_ctx_agent": host._ctx_agent,
                "_ctx_log": host._ctx_log,
                "_ctx_phase": host._ctx_phase,
                "_ctx_budget_total": host._ctx_budget_total,
                "_ctx_budget_spent": host._ctx_budget_spent,
                "_ctx_budget_remaining": host._ctx_budget_remaining,
            },
            limits=cast(ResourceLimits, limits) if limits else DEFAULT_LIMITS,
        )
    except FlowScriptError:
        raise
    except Exception as exc:
        raise FlowScriptError(_error_display(exc, user_lines, prelude_lines)) from exc
