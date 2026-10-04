"""Harness/provider pairings and non-secret launch plans."""

import shlex
from dataclasses import asdict

import pytest

from cacli import build_launch, get_harness, list_harnesses
from cacli.cli import check_credentials
from cacli.inference import PROVIDERS, provider_params


MATRIX = {
    "claude": ("claude-sub", "claude-api", "bedrock"),
    "codex": ("openai-sub", "openai-api", "bedrock", "azure", "workers-ai"),
    "pi": ("workers-ai", "claude-api", "openai-api"),
    "opencode": ("workers-ai", "claude-api", "openai-api"),
    "cursor": ("cursor",),
    "devin": ("devin",),
}
PARAMS = {
    "region": "us-west-2",
    "account_id": "account-123",
    "azure_host": "res.cognitiveservices.azure.com",
}


def test_full_support_matrix_and_unsupported_pairs():
    assert set(list_harnesses()) == set(MATRIX)
    assert "gemini" not in list_harnesses()
    assert "openai" not in list_harnesses()
    for harness, supported in MATRIX.items():
        adapter = get_harness(harness)
        assert tuple(pair.provider for pair in adapter.pairings) == supported
        for provider in PROVIDERS:
            if provider in supported:
                assert (
                    build_launch(
                        harness,
                        "hello",
                        provider=provider,
                        params=PARAMS,
                        model="example-model",
                    ).provider
                    == provider
                )
            else:
                with pytest.raises(
                    ValueError, match="Supported: " + ", ".join(supported)
                ):
                    build_launch(harness, "hello", provider=provider, params=PARAMS)


def test_exact_claude_subscription_command_and_unsets():
    launch = build_launch("claude", "hello", provider="claude-sub")
    assert launch.command == (
        "env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN "
        "-u ANTHROPIC_BASE_URL -u CLAUDE_CODE_USE_BEDROCK "
        "claude -p hello --verbose --output-format=stream-json"
    )
    assert launch.unset_env == (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
    )
    assert launch.credentials[0].env == "CLAUDE_CODE_OAUTH_TOKEN"


def test_bedrock_and_workers_bindings_resolve_parameters():
    bedrock = build_launch(
        "codex",
        "hello",
        provider="bedrock",
        params=PARAMS,
        model="anthropic.claude-example",
    )
    assert (
        "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1" in bedrock.command
    )
    assert bedrock.credentials[0].binding.hosts == (
        "bedrock-runtime.us-west-2.amazonaws.com",
    )
    workers = build_launch("pi", "hello", params=PARAMS)
    assert workers.command == (
        "printf '%s' hello | env CLOUDFLARE_ACCOUNT_ID=account-123 "
        "pi --mode json --print --no-session --approve "
        "--model cloudflare-workers-ai/@cf/zai-org/glm-5.3"
    )
    assert workers.credentials[0].binding.path_prefixes == (
        "/client/v4/accounts/account-123/ai/",
    )
    assert workers.credentials[0].binding.methods == ("GET", "POST")
    opencode = build_launch(
        "opencode", "hello", params=PARAMS, settings="/tmp/config.json"
    )
    assert opencode.command.startswith(
        "printf '%s' hello | env CLOUDFLARE_ACCOUNT_ID=account-123 OPENCODE_CONFIG="
    )


def test_missing_params_and_secret_values_never_enter_launch(monkeypatch):
    with pytest.raises(ValueError, match="account_id.*CLOUDFLARE_ACCOUNT_ID"):
        build_launch("pi", "hi")
    with pytest.raises(ValueError, match="region.*AWS_REGION"):
        build_launch("claude", "hi", provider="bedrock")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SECRET-SENTINEL")
    launch = build_launch("claude", "hi", provider="claude-api")
    assert "SECRET-SENTINEL" not in repr(asdict(launch))
    assert "SECRET-SENTINEL" not in launch.command
    assert "ANTHROPIC_API_KEY" not in launch.env


def test_cli_checks_env_and_file_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    launch = build_launch("claude", "hello", provider="claude-api")
    with pytest.raises(ValueError, match="ANTHROPIC_API_KEY") as exc:
        check_credentials(launch)
    assert "secret" not in str(exc.value).lower()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "SECRET-SENTINEL")
    check_credentials(launch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    file_launch = build_launch("codex", "hello")
    assert file_launch.credentials[0].file == "~/.codex/auth.json"
    with pytest.raises(ValueError, match="auth.json"):
        check_credentials(file_launch)
    (tmp_path / "auth.json").touch()
    check_credentials(file_launch)


def test_codex_custom_config_uses_responses_without_secret_value():
    launch = build_launch("codex", "hello", provider="openai-api")
    parts = shlex.split(launch.command)
    assert "model_provider=openai_api" in parts
    assert "model_providers.openai_api.wire_api=responses" in parts
    assert "model_providers.openai_api.env_key=OPENAI_API_KEY" in parts
    assert "model_providers.openai_api.base_url=https://api.openai.com/v1" in parts
    assert get_harness("codex").pairing("bedrock").verified is False
    assert get_harness("codex").pairing("workers-ai").verified is False


def test_pr_description_uses_pairing_and_env_prefix():
    from cacli import build_pr_description_command

    command = build_pr_description_command(
        "claude", "summarize", provider="bedrock", params={"region": "us-east-1"}
    )
    assert command.startswith(
        "env -u ANTHROPIC_API_KEY -u CLAUDE_CODE_OAUTH_TOKEN "
        "CLAUDE_CODE_USE_BEDROCK=1 AWS_REGION=us-east-1 claude -p summarize"
    )
    codex = build_pr_description_command("codex", "summarize", provider="openai-api")
    assert "model_providers.openai_api.wire_api=responses" in codex


def test_cli_rejects_missing_credential_without_printing_value(monkeypatch, capsys):
    import sys
    from cacli.cli import main

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["cacli", "run", "hello", "--harness", "claude", "--provider", "claude-api"],
    )
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "ANTHROPIC_API_KEY" in captured.err
    assert "SECRET-SENTINEL" not in captured.err


def test_gemini_module_and_cost_are_gone():
    import importlib.util
    from cacli.flow.costs import FALLBACK_COSTS

    assert importlib.util.find_spec("cacli.harnesses.gemini") is None
    assert "gemini" not in FALLBACK_COSTS


@pytest.mark.parametrize("harness", ["pi", "opencode", "devin", "claude", "codex"])
@pytest.mark.parametrize(
    "prompt",
    [
        "words | more words",
        "words -- devin more words",
        "words env -u TOKEN",
        "quotes: 'single' and \"double\"",
        "first line\nsecond line",
    ],
)
@pytest.mark.parametrize("pr_description", [False, True])
def test_adapter_prefix_stays_before_binary_for_adversarial_prompts(
    harness, prompt, pr_description
):
    adapter = get_harness(harness)
    prefix = ("env", "-u", "SHADOW_KEY", "NONSECRET=value")
    if pr_description:
        command = adapter.build_pr_description_command(prompt, exec_prefix=prefix)
    else:
        command = adapter.build_command(prompt, exec_prefix=prefix)
    tokens = shlex.split(command)
    binary = {"cursor": "agent"}.get(harness, harness)
    position = tokens.index(binary)
    assert tokens[position - len(prefix) : position] == list(prefix)
    assert prompt in tokens
    if harness in {"pi", "opencode"}:
        assert tokens.index("|") < position
    if harness == "devin" and not pr_description:
        assert tokens.index("--") < position


def test_codex_non_openai_pairs_require_explicit_model():
    for provider in ("bedrock", "azure", "workers-ai"):
        with pytest.raises(ValueError, match=f"model is required.*{provider}"):
            build_launch("codex", "hello", provider=provider, params=PARAMS)
        launch = build_launch(
            "codex", "hello", provider=provider, model="provider-model", params=PARAMS
        )
        assert "--model provider-model" in launch.command
        assert "model_reasoning_effort=high" not in launch.command
    assert "--model gpt-5.4" in build_launch("codex", "hello").command


def test_catalog_owns_openai_compatible_endpoints():
    assert PROVIDERS["openai-api"].openai_base_url == "https://api.openai.com/v1"
    assert PROVIDERS["bedrock"].openai_base_url == (
        "https://bedrock-runtime.{region}.amazonaws.com/openai/v1"
    )
    assert PROVIDERS["workers-ai"].openai_base_url == (
        "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"
    )


def test_claude_sub_accepts_local_login_without_env(monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    launch = build_launch("claude", "hello")
    assert launch.provider == "claude-sub"
    assert launch.credentials[0].local_login == "claude /login"
    check_credentials(launch)
    assert "ANTHROPIC_API_KEY" in launch.unset_env


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://res.cognitiveservices.azure.com/",
        "https://res.cognitiveservices.azure.com/openai/v1",
        "res.cognitiveservices.azure.com",
    ],
)
def test_azure_endpoint_resolves_to_host(endpoint):
    launch = build_launch(
        "codex",
        "hello",
        provider="azure",
        model="gpt-5.3-codex",
        params={"azure_host": endpoint},
    )
    assert (
        "model_providers.azure.base_url=https://res.cognitiveservices.azure.com/openai/v1"
        in launch.command
    )
    assert "model_providers.azure.env_key=AZURE_OPENAI_API_KEY" in launch.command
    (credential,) = launch.credentials
    assert credential.binding.hosts == ("res.cognitiveservices.azure.com",)
    assert credential.binding.header == "Authorization"


def test_azure_requires_endpoint():
    with pytest.raises(ValueError, match="azure_host.*AZURE_OPENAI_ENDPOINT"):
        build_launch("codex", "hello", provider="azure", model="m", params={})


def test_provider_params_reads_every_param(monkeypatch):
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    params = provider_params({"AZURE_OPENAI_ENDPOINT": "https://res.openai.azure.com/"})
    assert params == {
        "region": "us-east-1",
        "account_id": "",
        "azure_host": "https://res.openai.azure.com/",
    }
