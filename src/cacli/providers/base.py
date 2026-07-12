"""Abstract base class for coding agent providers."""

from abc import ABC, abstractmethod

from cacli.types import AgentRunResult


class BaseProvider(ABC):
    """Abstract base for all coding agent providers."""

    name: str

    @abstractmethod
    def build_command(
        self,
        prompt: str,
        model: str | None = None,
        reasoning_effort: str | None = None,
        web_search: bool = True,
        settings: str | None = None,
        sandbox: bool | None = None,
        output_schema: dict | None = None,
    ) -> str:
        """Build the shell command string to invoke this provider.

        ``sandbox`` requests OS-level isolation where the provider supports
        it (currently only Devin). ``None`` means provider default.
        ``output_schema`` is reserved for workflow structured output; Phase 1
        providers intentionally ignore it.
        """
        ...

    @abstractmethod
    def parse_output(self, raw_output: str) -> AgentRunResult:
        """Parse raw output and extract result message, cost, etc."""
        ...

    @abstractmethod
    def build_initial_log_entry(self, prompt: str, model: str | None = None) -> str:
        """Build the initial JSONL log entry for this provider's format."""
        ...

    @abstractmethod
    def build_pr_description_command(
        self,
        prompt: str,
        model: str | None = None,
        settings: str | None = None,
        sandbox: bool | None = None,
    ) -> str:
        """Build command for generating PR descriptions."""
        ...

    @abstractmethod
    def extract_result_from_json(self, raw_output: str) -> str:
        """Extract the result text from JSON output."""
        ...

    def resolve_model(self, model: str | None) -> str | None:
        """Resolve model aliases. Override in providers with aliases."""
        return model
