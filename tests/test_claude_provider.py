import json
import shlex

import pytest

from cacli.harnesses.claude import ClaudeHarness


def test_resolve_model_expands_claude_version_aliases():
    provider = ClaudeHarness()

    assert provider.resolve_model("fable-5") == "claude-fable-5"
    assert provider.resolve_model("opus-4.8") == "claude-opus-4-8"
    assert provider.resolve_model("opus-4-7") == "claude-opus-4-7"
    assert provider.resolve_model("sonnet-4.6") == "claude-sonnet-4-6"


def test_resolve_model_preserves_claude_aliases_and_provider_ids():
    provider = ClaudeHarness()

    assert provider.resolve_model("fable") == "fable"
    assert provider.resolve_model("opus") == "opus"
    assert (
        provider.resolve_model("us.anthropic.claude-opus-4-8")
        == "us.anthropic.claude-opus-4-8"
    )


def test_build_command_uses_resolved_model():
    provider = ClaudeHarness()

    cmd = provider.build_command("hello", model=provider.resolve_model("opus-4.8"))

    assert "--model claude-opus-4-8" in cmd


def test_build_command_maps_schema_and_sandbox_settings():
    provider = ClaudeHarness()
    schema = {"type": "object", "required": ["answer"]}
    command = provider.build_command("hello", output_schema=schema, sandbox=True)
    parts = shlex.split(command)
    assert parts[parts.index("--json-schema") + 1] == json.dumps(
        schema, sort_keys=True, separators=(",", ":")
    )
    assert json.loads(parts[parts.index("--settings") + 1]) == {
        "sandbox": {
            "enabled": True,
            "autoAllowBashIfSandboxed": True,
            "failIfUnavailable": True,
        }
    }
    disabled = shlex.split(provider.build_command("hello", sandbox=False))
    assert json.loads(disabled[disabled.index("--settings") + 1]) == {
        "sandbox": {"enabled": False}
    }
    with pytest.raises(ValueError, match="cannot be combined"):
        provider.build_command("hello", sandbox=True, settings="settings.json")


def test_parse_output_extracts_structured_output():
    result = ClaudeHarness().parse_output(
        json.dumps(
            {
                "type": "result",
                "result": "text",
                "structured_output": {"answer": 42},
            }
        )
    )
    assert result.structured_output == {"answer": 42}
