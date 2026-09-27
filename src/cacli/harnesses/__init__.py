"""Registry of coding agent CLI harnesses."""

from cacli.harnesses.base import BaseHarness
from cacli.harnesses.claude import ClaudeHarness
from cacli.harnesses.codex import CodexHarness
from cacli.harnesses.cursor import CursorHarness
from cacli.harnesses.devin import DevinHarness
from cacli.harnesses.opencode import OpenCodeHarness
from cacli.harnesses.pi import PiHarness

_HARNESSES: dict[str, BaseHarness] = {
    harness.name: harness
    for harness in (
        ClaudeHarness(),
        CodexHarness(),
        PiHarness(),
        OpenCodeHarness(),
        CursorHarness(),
        DevinHarness(),
    )
}


def get_harness(name: str) -> BaseHarness:
    if name not in _HARNESSES:
        raise ValueError(
            f"Unknown harness: {name}. Available: {', '.join(sorted(_HARNESSES))}"
        )
    return _HARNESSES[name]


def list_harnesses() -> list[str]:
    return sorted(_HARNESSES)
