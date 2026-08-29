import json
import shlex

import pytest

from cacli.providers import list_providers
from cacli.providers.pi import PiProvider
from cacli.runner import run_agent
from cacli.types import ExecResult


MODEL = "cloudflare-ai-gateway/workers-ai/@cf/zai-org/glm-5.3"


def test_pi_provider_is_registered():
    assert "pi" in list_providers()


def test_build_command_uses_safe_headless_mode_and_qualified_model():
    command = PiProvider().build_command(
        "fix the tests; echo not-a-command",
        model=MODEL,
        reasoning_effort="high",
        sandbox=False,
    )

    assert shlex.split(command) == [
        "pi",
        "--mode",
        "json",
        "--print",
        "--no-session",
        "--approve",
        "--model",
        MODEL,
        "--thinking",
        "high",
        "fix the tests; echo not-a-command",
    ]


def test_build_command_omits_optional_model_and_auto_thinking():
    parts = shlex.split(PiProvider().build_command("hello", reasoning_effort="auto"))

    assert "--model" not in parts
    assert "--thinking" not in parts


def test_build_command_rejects_internal_sandbox_and_settings():
    provider = PiProvider()

    with pytest.raises(NotImplementedError, match="external sandboxing"):
        provider.build_command("hello", sandbox=True)
    with pytest.raises(ValueError, match="settings argument"):
        provider.build_command("hello", settings="settings.json")


def test_parse_output_extracts_last_assistant_text_and_sums_costs():
    events = [
        {"type": "session", "version": 3, "id": "run"},
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "private"},
                    {"type": "text", "text": "I will inspect it."},
                    {"type": "toolCall", "id": "1", "name": "read"},
                ],
                "usage": {"cost": {"total": 0.125}},
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "toolResult",
                "content": [{"type": "text", "text": "tool output"}],
            },
        },
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Done."},
                    {"type": "text", "text": "Tests pass."},
                ],
                "usage": {"cost": {"total": 0.375}},
            },
        },
    ]
    raw_output = "not json\n" + "\n".join(json.dumps(event) for event in events)

    result = PiProvider().parse_output(raw_output)

    assert result.result_message == "Done.\nTests pass."
    assert result.total_cost == pytest.approx(0.5)


def test_parse_output_tolerates_missing_or_invalid_cost():
    raw_output = "\n".join(
        [
            json.dumps(
                {
                    "type": "message_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "Done"}],
                        "usage": {"cost": {"total": "unknown"}},
                    },
                }
            ),
            '{"type":"ccremote.event"}',
        ]
    )

    result = PiProvider().parse_output(raw_output)

    assert result.result_message == "Done"
    assert result.total_cost is None


def test_parse_output_surfaces_provider_error():
    output = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": '401 [{"code":2009,"message":"Unauthorized"}]',
                "usage": {"cost": {"total": 0}},
            },
        }
    )

    result = PiProvider().parse_output(output)

    assert result.exit_code == 1
    assert result.result_message == '401 [{"code":2009,"message":"Unauthorized"}]'
    assert result.total_cost == 0


def test_run_agent_preserves_structured_provider_error_exit_code():
    output = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [],
                "stopReason": "error",
                "errorMessage": "provider unavailable",
            },
        }
    )

    result = run_agent(
        provider="pi",
        prompt="hello",
        exec_fn=lambda *_args: ExecResult(exit_code=0, stdout=output),
    )

    assert result.exit_code == 1
    assert result.result_message == "provider unavailable"


def test_initial_log_entry_and_extract_result():
    provider = PiProvider()
    initial = json.loads(provider.build_initial_log_entry("hello", MODEL))

    assert initial == {
        "type": "user",
        "provider": "pi",
        "message": {"role": "user", "content": "hello"},
        "model": MODEL,
    }

    output = json.dumps(
        {
            "type": "message_end",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "final"}],
            },
        }
    )
    assert provider.extract_result_from_json(output) == "final"
