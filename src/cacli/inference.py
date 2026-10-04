"""Inference providers, required parameters, and credential wire metadata."""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Param:
    name: str
    env: str
    normalize: Callable[[str], str] | None = None

    def read(self, env: Mapping[str, str] | None = None) -> str:
        return (env or {}).get(self.env, os.environ.get(self.env, ""))


@dataclass(frozen=True)
class WireBinding:
    hosts: tuple[str, ...]
    header: str | None = None
    value_format: str | None = None
    query_param: str | None = None
    methods: tuple[str, ...] = ()
    path_prefixes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (self.header is None) == (self.query_param is None):
            raise ValueError("wire binding needs either a header or query parameter")
        if self.header is not None and (
            self.value_format is None or "{secret}" not in self.value_format
        ):
            raise ValueError("header binding needs a value format containing {secret}")

    def resolve(self, params: dict[str, str]) -> "WireBinding":
        return replace(
            self,
            hosts=tuple(host.format(**params) for host in self.hosts),
            path_prefixes=tuple(path.format(**params) for path in self.path_prefixes),
        )


@dataclass(frozen=True)
class Credential:
    env: str | None = None
    file: str | None = None
    binding: WireBinding | None = None
    unbrokerable_reason: str | None = None
    # The harness can instead use its own interactive login (e.g. `claude
    # /login` stored in the keychain). Local presence checks then treat a
    # missing env var as fine; sandboxes still need the env var.
    local_login: str | None = None

    def __post_init__(self) -> None:
        if (self.env is None) == (self.file is None):
            raise ValueError("credential needs exactly one of env or file")
        if (self.binding is None) == (self.unbrokerable_reason is None):
            raise ValueError("credential needs exactly one of binding or reason")

    def resolved(self, params: dict[str, str]) -> "Credential":
        return replace(
            self,
            binding=self.binding.resolve(params) if self.binding else None,
        )


@dataclass(frozen=True)
class Provider:
    name: str
    params: tuple[Param, ...]
    credentials: tuple[Credential, ...]
    openai_base_url: str | None = None


def _hostname(endpoint: str) -> str:
    """Accept a bare host or a portal endpoint URL like https://x.openai.azure.com/."""
    return urlsplit(endpoint if "//" in endpoint else f"//{endpoint}").hostname or ""


REGION = Param("region", "AWS_REGION")
ACCOUNT_ID = Param("account_id", "CLOUDFLARE_ACCOUNT_ID")
# Any of the resource's hosts works: *.openai.azure.com,
# *.cognitiveservices.azure.com, or *.services.ai.azure.com.
AZURE_HOST = Param("azure_host", "AZURE_OPENAI_ENDPOINT", normalize=_hostname)

PROVIDERS = {
    "claude-sub": Provider(
        "claude-sub",
        (),
        (
            Credential(
                env="CLAUDE_CODE_OAUTH_TOKEN",
                local_login="claude /login",
                binding=WireBinding(
                    ("api.anthropic.com",),
                    header="Authorization",
                    value_format="Bearer {secret}",
                ),
            ),
        ),
    ),
    "claude-api": Provider(
        "claude-api",
        (),
        (
            Credential(
                env="ANTHROPIC_API_KEY",
                binding=WireBinding(
                    ("api.anthropic.com",), header="x-api-key", value_format="{secret}"
                ),
            ),
        ),
    ),
    "openai-sub": Provider(
        "openai-sub",
        (),
        (
            Credential(
                file="~/.codex/auth.json",
                unbrokerable_reason="OAuth refresh tokens are rotated by Codex",
            ),
        ),
    ),
    "openai-api": Provider(
        "openai-api",
        (),
        (
            Credential(
                env="OPENAI_API_KEY",
                binding=WireBinding(
                    ("api.openai.com",),
                    header="Authorization",
                    value_format="Bearer {secret}",
                ),
            ),
        ),
        "https://api.openai.com/v1",
    ),
    "bedrock": Provider(
        "bedrock",
        (REGION,),
        (
            Credential(
                env="AWS_BEARER_TOKEN_BEDROCK",
                binding=WireBinding(
                    ("bedrock-runtime.{region}.amazonaws.com",),
                    header="Authorization",
                    value_format="Bearer {secret}",
                ),
            ),
        ),
        "https://bedrock-runtime.{region}.amazonaws.com/openai/v1",
    ),
    "azure": Provider(
        "azure",
        (AZURE_HOST,),
        (
            Credential(
                env="AZURE_OPENAI_API_KEY",
                binding=WireBinding(
                    ("{azure_host}",),
                    header="Authorization",
                    value_format="Bearer {secret}",
                ),
            ),
        ),
        "https://{azure_host}/openai/v1",
    ),
    "workers-ai": Provider(
        "workers-ai",
        (ACCOUNT_ID,),
        (
            Credential(
                env="CLOUDFLARE_API_KEY",
                binding=WireBinding(
                    ("api.cloudflare.com",),
                    header="Authorization",
                    value_format="Bearer {secret}",
                    methods=("GET", "POST"),
                    path_prefixes=("/client/v4/accounts/{account_id}/ai/",),
                ),
            ),
        ),
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1",
    ),
    "cursor": Provider(
        "cursor",
        (),
        (
            Credential(
                env="CURSOR_API_KEY",
                unbrokerable_reason="Cursor wire format is not documented",
            ),
        ),
    ),
    "devin": Provider(
        "devin",
        (),
        (
            Credential(
                file="~/.local/share/devin/credentials.toml",
                unbrokerable_reason="Devin login credentials are managed by the CLI",
            ),
        ),
    ),
}


def provider_params(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Read every provider parameter from env, falling back to os.environ."""
    return {
        param.name: param.read(env)
        for provider in PROVIDERS.values()
        for param in provider.params
    }
