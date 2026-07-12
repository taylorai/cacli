"""Durable workflow journals, order-chained keys, and resume caches."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


KEY_SEED = "cacli-flow-journal-v1"


def canonical_json(value: Any) -> str:
    """Encode a value identically across runs and Python formatting choices."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class JournalKeyChain:
    """Assign call keys whose identity includes the entire preceding call order."""

    def __init__(self, seed: str = KEY_SEED):
        self.previous = seed

    def next(self, call: dict[str, Any]) -> str:
        payload = self.previous + canonical_json(call)
        key = hashlib.sha256(payload.encode()).hexdigest()
        self.previous = key
        return key


def read_journal(path: Path) -> list[dict[str, Any]]:
    """Read valid JSON objects from a journal, tolerating a torn final line."""
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def append_journal(path: Path, entry: dict[str, Any]) -> None:
    """Append one compact entry to a journal."""
    with path.open("a") as handle:
        handle.write(json.dumps(entry, separators=(",", ":")) + "\n")


@dataclass
class ResumeCache:
    """Terminal results and incomplete attempts from an earlier run."""

    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    started: set[str] = field(default_factory=set)
    replaying: bool = True

    @classmethod
    def from_journal(cls, path: Path) -> "ResumeCache":
        cache = cls()
        for entry in read_journal(path):
            key = entry.get("key")
            if not isinstance(key, str):
                continue
            if entry.get("type") == "started":
                cache.started.add(key)
            elif entry.get("type") == "result":
                cache.results[key] = entry
                cache.started.discard(key)
        return cache
