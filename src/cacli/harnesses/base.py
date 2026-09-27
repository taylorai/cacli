"""Base class and provider pairing for coding agent harnesses."""

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

import shlex

from cacli.types import AgentRunResult


@dataclass(frozen=True)
class Pairing:
    provider: str
    credentials: tuple[str, ...] = ()
    env: tuple[tuple[str, str], ...] = ()
    unset_env: tuple[str, ...] = ()
    model_prefix: str | None = None
    default_model: str | None = None
    default_reasoning_effort: str | None = None
    requires_model: bool = False
    verified: bool = True


class BaseHarness(ABC):
    name: str
    pairings: tuple[Pairing, ...]

    def pairing(self, provider: str | None) -> Pairing:
        selected = provider or self.pairings[0].provider
        for pair in self.pairings:
            if pair.provider == selected:
                return pair
        supported = ", ".join(pair.provider for pair in self.pairings)
        raise ValueError(
            f"Harness {self.name!r} does not support provider {selected!r}. Supported: {supported}"
        )

    def configure_command(
        self, pair: Pairing, command: str, params: dict[str, str]
    ) -> str:
        return command

    @staticmethod
    def command_head(binary: str, exec_prefix: Sequence[str]) -> str:
        return shlex.join([*exec_prefix, binary])

    @abstractmethod
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
    ) -> str: ...

    @abstractmethod
    def parse_output(self, raw_output: str) -> AgentRunResult: ...

    @abstractmethod
    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str: ...

    @abstractmethod
    def build_pr_description_command(
        self,
        prompt: str,
        model: str | None = None,
        settings: str | None = None,
        sandbox: bool | None = None,
        exec_prefix: Sequence[str] = (),
    ) -> str: ...

    @abstractmethod
    def extract_result_from_json(self, raw_output: str) -> str: ...

    def resolve_model(self, model: str | None) -> str | None:
        return model
