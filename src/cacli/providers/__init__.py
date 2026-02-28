"""Provider registry."""

from cacli.providers.base import BaseProvider
from cacli.providers.claude import ClaudeProvider
from cacli.providers.codex import CodexProvider
from cacli.providers.cursor import CursorProvider
from cacli.providers.gemini import GeminiProvider

_PROVIDERS: dict[str, BaseProvider] = {}


def _init_registry() -> None:
    _PROVIDERS["claude"] = ClaudeProvider()
    _PROVIDERS["codex"] = CodexProvider()
    _PROVIDERS["openai"] = CodexProvider()
    _PROVIDERS["gemini"] = GeminiProvider()
    _PROVIDERS["cursor"] = CursorProvider()


_init_registry()


def get_provider(name: str) -> BaseProvider:
    """Get a provider instance by name."""
    if name not in _PROVIDERS:
        available = ", ".join(sorted(_PROVIDERS))
        raise ValueError(f"Unknown provider: {name}. Available: {available}")
    return _PROVIDERS[name]


def list_providers() -> list[str]:
    """List all available provider names."""
    return sorted(_PROVIDERS.keys())
