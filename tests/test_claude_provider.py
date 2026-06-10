from cacli.providers.claude import ClaudeProvider


def test_resolve_model_expands_claude_version_aliases():
    provider = ClaudeProvider()

    assert provider.resolve_model("fable-5") == "claude-fable-5"
    assert provider.resolve_model("opus-4.8") == "claude-opus-4-8"
    assert provider.resolve_model("opus-4-7") == "claude-opus-4-7"
    assert provider.resolve_model("sonnet-4.6") == "claude-sonnet-4-6"


def test_resolve_model_preserves_claude_aliases_and_provider_ids():
    provider = ClaudeProvider()

    assert provider.resolve_model("fable") == "fable"
    assert provider.resolve_model("opus") == "opus"
    assert (
        provider.resolve_model("us.anthropic.claude-opus-4-8")
        == "us.anthropic.claude-opus-4-8"
    )


def test_build_command_uses_resolved_model():
    provider = ClaudeProvider()

    cmd = provider.build_command("hello", model=provider.resolve_model("opus-4.8"))

    assert "--model claude-opus-4-8" in cmd
