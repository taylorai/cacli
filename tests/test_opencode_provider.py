import json
import os
import subprocess
import shlex

import pytest

from cacli.providers import list_providers
from cacli.runner import run_agent
from cacli.types import ExecResult
from cacli.providers.opencode import OPENCODE_DEFAULT_MODEL, OpenCodeProvider


def test_opencode_provider_is_registered():
    assert "opencode" in list_providers()


def test_build_command_defaults_to_glm_53_and_auto_approves():
    command = OpenCodeProvider().build_command(
        "fix the tests; echo not-a-command",
        sandbox=False,
    )

    assert shlex.split(command) == [
        "printf",
        "%s",
        "fix the tests; echo not-a-command",
        "|",
        "opencode",
        "run",
        "--format",
        "json",
        "--auto",
        "--model",
        OPENCODE_DEFAULT_MODEL,
    ]


def test_build_command_resolves_alias_and_forwards_variant_and_settings():
    command = OpenCodeProvider().build_command(
        "hello",
        model="glm-5.3",
        reasoning_effort="high",
        settings="/tmp/open code.json",
    )

    assert shlex.split(command) == [
        "printf",
        "%s",
        "hello",
        "|",
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

    assert parts[4:9] == ["opencode", "run", "--format", "json", "--auto"]


@pytest.mark.parametrize(
    "prompt",
    [
        "--help",
        "- Summarize these points",
        "@team please review",
        "Quotes: ' and \"; $(exit 9); `exit 8`; 100%\\n\nUnicode: café",
        "  keep whitespace\n\n",
    ],
)
def test_prompt_stdin_preserves_literal_content_and_exit_status(tmp_path, prompt):
    executable = tmp_path / "opencode"
    executable.write_text(
        "#!/bin/sh\ncat\nprintf '%s' \"$OPENCODE_CONFIG\" >&2\nexit 7\n"
    )
    executable.chmod(0o755)
    result = subprocess.run(
        OpenCodeProvider().build_command(prompt, settings="/tmp/open code.json"),
        shell=True,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )
    assert result.stdout == prompt
    assert result.stderr == "/tmp/open code.json"
    assert result.returncode == 7


def test_disabling_web_search_warns():
    with pytest.warns(UserWarning, match="web_search=False.*ignored"):
        OpenCodeProvider().build_command("test", web_search=False)


@pytest.mark.parametrize(
    "error,expected",
    [
        ({"data": {"message": "quota exhausted"}}, "quota exhausted"),
        ({}, "OpenCode provider returned an error"),
    ],
)
@pytest.mark.parametrize("process_exit", [0, 7])
def test_error_overrides_text_and_preserves_failure_and_cost(
    error, expected, process_exit
):
    events = [
        {"type": "text", "part": {"id": "p1", "messageID": "m1", "text": "partial"}},
        {"type": "step_finish", "part": {"id": "f1", "cost": 0.25}},
        {"type": "error", "error": error},
        # A later completed part notification must not erase a terminal error.
        {"type": "text", "part": {"id": "p2", "messageID": "m1", "text": "late text"}},
    ]
    raw = "\n".join(json.dumps(event) for event in events)
    parsed = OpenCodeProvider().parse_output(raw)
    assert parsed.exit_code == 1
    assert parsed.result_message == expected
    result = run_agent("opencode", "test", lambda *args: ExecResult(process_exit, raw))
    assert result.exit_code == (process_exit or 1)
    assert result.result_message == expected
    assert result.total_cost == pytest.approx(0.25)
