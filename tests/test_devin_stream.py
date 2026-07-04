import io
import json
import os
import stat
import textwrap

from cacli.devin_stream import (
    result_entry,
    run_devin_stream,
    step_to_entries,
)

AGENT_STEP = {
    "step_id": 9,
    "timestamp": "2026-06-23T21:46:18+00:00",
    "source": "agent",
    "message": "I'll read the file.",
    "model_name": "Claude Sonnet 5",
    "reasoning_content": "Need to inspect README first.",
    "tool_calls": [
        {
            "tool_call_id": "call-1",
            "function_name": "read",
            "arguments": {"path": "README.md"},
        }
    ],
    "observation": {
        "results": [{"source_call_id": "call-1", "content": "# cacli"}]
    },
}


def test_system_steps_produce_no_entries():
    assert step_to_entries({"step_id": 1, "source": "system", "message": "..."}) == []


def test_user_steps_are_skipped():
    # The prompt is the log writer's job (build_initial_log_entry); emitting
    # the ATIF user step too would duplicate it in viewers.
    entries = step_to_entries({"step_id": 7, "source": "user", "message": "do it"})

    assert entries == []


def test_agent_step_becomes_assistant_and_tool_result_entries():
    entries = step_to_entries(AGENT_STEP)

    assert len(entries) == 2
    assistant, tool_result = entries

    assert assistant["type"] == "assistant"
    content = assistant["message"]["content"]
    assert content[0] == {
        "type": "thinking",
        "thinking": "Need to inspect README first.",
    }
    assert content[1] == {"type": "text", "text": "I'll read the file."}
    assert content[2] == {
        "type": "tool_use",
        "id": "call-1",
        "name": "read",
        "input": {"path": "README.md"},
    }

    assert tool_result["type"] == "user"
    assert tool_result["message"]["content"] == [
        {"type": "tool_result", "tool_use_id": "call-1", "content": "# cacli"}
    ]


def test_result_entry_uses_last_agent_message():
    export = {
        "session_id": "test-session",
        "final_metrics": {"prompt_tokens": 10},
        "steps": [
            {"step_id": 1, "source": "system", "message": "sys"},
            {"step_id": 2, "source": "user", "message": "go"},
            dict(AGENT_STEP, step_id=3),
            {"step_id": 4, "source": "agent", "message": "Final answer."},
        ],
    }

    entry = result_entry(export, "stdout fallback")

    assert entry["type"] == "result"
    assert entry["result"] == "Final answer."
    assert entry["num_turns"] == 2
    assert entry["session_id"] == "test-session"
    assert entry["usage"] == {"prompt_tokens": 10}


def test_result_entry_falls_back_to_stdout():
    assert result_entry(None, "plain text")["result"] == "plain text"


def _write_fake_devin(tmp_path):
    """A fake devin that writes its export in two stages, then prints text."""
    export_template = {
        "schema_version": "ATIF-v1.7",
        "session_id": "fake-session",
        "agent": {"name": "devin", "model_name": "Claude Sonnet 5"},
        "steps": [
            {"step_id": 1, "source": "system", "message": "system prompt"},
            {"step_id": 2, "source": "user", "message": "read the readme"},
            AGENT_STEP,
        ],
    }
    script = tmp_path / "fake_devin.py"
    script.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json, sys, time

            args = sys.argv[1:]
            export_path = args[args.index("--export") + 1]
            export = {json.dumps(export_template)}
            with open(export_path, "w") as f:
                json.dump(export, f)
            time.sleep(0.3)
            export["steps"].append(
                {{"step_id": 10, "source": "agent", "message": "Final answer."}}
            )
            export["final_metrics"] = {{"prompt_tokens": 42}}
            with open(export_path, "w") as f:
                json.dump(export, f)
            print("Final answer.")
            """
        )
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def test_run_devin_stream_emits_stream_json(tmp_path):
    script = _write_fake_devin(tmp_path)
    export_path = os.path.join(tmp_path, "export.json")
    out = io.StringIO()

    exit_code = run_devin_stream(
        [str(script), "--permission-mode", "dangerous", "--print", "read the readme"],
        export_path=export_path,
        poll_interval=0.05,
        stdout=out,
    )

    assert exit_code == 0
    entries = [json.loads(line) for line in out.getvalue().splitlines()]
    types = [(e["type"], e.get("subtype")) for e in entries]

    assert types[0] == ("system", "init")
    assert entries[0]["session_id"] == "fake-session"
    # The only user entries are tool results — the prompt itself is not echoed
    user_entries = [e for e in entries if e["type"] == "user"]
    assert user_entries
    assert all(
        block["type"] == "tool_result"
        for e in user_entries
        for block in e["message"]["content"]
    )
    assert any(e["type"] == "assistant" for e in entries)

    result = entries[-1]
    assert result["type"] == "result"
    assert result["result"] == "Final answer."
    assert result["usage"] == {"prompt_tokens": 42}

    tool_uses = [
        block
        for e in entries
        if e["type"] == "assistant"
        for block in e["message"]["content"]
        if block["type"] == "tool_use"
    ]
    assert tool_uses == [
        {"type": "tool_use", "id": "call-1", "name": "read", "input": {"path": "README.md"}}
    ]


def test_run_devin_stream_reports_launch_failure(tmp_path):
    out = io.StringIO()
    exit_code = run_devin_stream(
        [os.path.join(tmp_path, "does-not-exist")],
        export_path=os.path.join(tmp_path, "export.json"),
        stdout=out,
    )

    assert exit_code == 127
    entries = [json.loads(line) for line in out.getvalue().splitlines()]
    assert entries[-1]["type"] == "result"
    assert entries[-1]["is_error"] is True
