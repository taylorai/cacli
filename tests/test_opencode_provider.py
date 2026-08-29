import json
import shlex

import pytest

from cacli.providers import list_providers
from cacli.providers.opencode import OPENCODE_DEFAULT_MODEL, OpenCodeProvider


def test_opencode_provider_is_registered():
    assert "opencode" in list_providers()


def test_build_command_defaults_to_glm_53_and_auto_approves():
    command = OpenCodeProvider().build_command(
        "fix the tests; echo not-a-command",
        sandbox=False,
    )

    assert shlex.split(command) == [
        "opencode",
        "run",
        "--format",
        "json",
        "--auto",
        "--model",
        OPENCODE_DEFAULT_MODEL,
        "fix the tests; echo not-a-command",
    ]


def test_build_command_resolves_alias_and_forwards_variant_and_settings():
    command = OpenCodeProvider().build_command(
        "hello",
        model="glm-5.3",
        reasoning_effort="high",
        settings="/tmp/open code.json",
    )

    assert shlex.split(command) == [
        "env",
        "OPENCODE_CONFIG=/tmp/open code.json",
        "opencode",
        "run",
        "--format",
        "json",
        "--auto",
        "--model",
        OPENCODE_DEFAULT_MODEL,
        "--variant",
        "high",
        "hello",
    ]


def test_build_command_preserves_qualified_models_and_omits_auto_variant():
    model = "togetherai/zai-org/GLM-5.3-Flash"
    command = OpenCodeProvider().build_command(
        "hello",
        model=model,
        reasoning_effort="auto",
    )

    parts = shlex.split(command)
    assert parts[parts.index("--model") + 1] == model
    assert "--variant" not in parts


def test_build_command_rejects_internal_sandbox():
    with pytest.raises(NotImplementedError, match="external sandboxing"):
        OpenCodeProvider().build_command("hello", sandbox=True)


def test_parse_output_extracts_last_message_text_and_sums_costs():
    events = [
        {
            "type": "text",
            "part": {
                "id": "part-1",
                "messageID": "message-1",
                "type": "text",
                "text": "I will inspect it.",
            },
        },
        {
            "type": "step_finish",
            "part": {"id": "finish-1", "type": "step-finish", "cost": 0.125},
        },
        {
            "type": "text",
            "part": {
                "id": "part-2",
                "messageID": "message-2",
                "type": "text",
                "text": "Done.",
            },
        },
        {
            "type": "text",
            "part": {
                "id": "part-3",
                "messageID": "message-2",
                "type": "text",
                "text": "Tests pass.",
            },
        },
        {
            "type": "step_finish",
            "part": {"id": "finish-2", "type": "step-finish", "cost": 0.375},
        },
    ]
    # Repeated completed-part notifications must not double-count output or cost.
    events.extend([events[-2], events[-1]])
    raw_output = "not json\n" + "\n".join(json.dumps(event) for event in events)

    result = OpenCodeProvider().parse_output(raw_output)

    assert result.result_message == "Done.\nTests pass."
    assert result.total_cost == pytest.approx(0.5)


def test_parse_output_uses_error_when_no_text_and_tolerates_invalid_cost():
    raw_output = "\n".join(
        [
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {"id": "finish", "cost": "unknown"},
                }
            ),
            json.dumps(
                {
                    "type": "error",
                    "error": {
                        "name": "ProviderError",
                        "data": {"message": "quota exhausted"},
                    },
                }
            ),
        ]
    )

    result = OpenCodeProvider().parse_output(raw_output)

    assert result.result_message == "quota exhausted"
    assert result.total_cost is None


def test_initial_log_entry_extract_result_and_skills_dir():
    provider = OpenCodeProvider()
    initial = json.loads(provider.build_initial_log_entry("hello", "glm-5.3"))

    assert initial == {
        "type": "user",
        "provider": "opencode",
        "message": {"role": "user", "content": "hello"},
        "model": OPENCODE_DEFAULT_MODEL,
    }

    output = json.dumps(
        {
            "type": "text",
            "part": {
                "id": "part",
                "messageID": "message",
                "type": "text",
                "text": "final",
            },
        }
    )
    assert provider.extract_result_from_json(output) == "final"
    assert provider.skills_dir() == "/root/.config/opencode/skills"


def test_pr_description_command_uses_structured_headless_mode():
    parts = shlex.split(OpenCodeProvider().build_pr_description_command("describe"))

    assert parts[:5] == ["opencode", "run", "--format", "json", "--auto"]
