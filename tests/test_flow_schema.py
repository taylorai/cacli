import asyncio
import json
import shlex
from pathlib import Path

import pytest

from cacli.flow import FlowRunFailed, run_flow
from cacli.flow.aexec import AsyncExecResult
from cacli.flow.journal import JournalKeyChain, read_journal
from cacli.flow.schema import extract_json
from cacli.harnesses import _HARNESSES
from cacli.harnesses.base import BaseHarness, Pairing
from cacli.types import AgentRunResult


class SchemaFakeHarness(BaseHarness):
    pairings = (Pairing("openai-api"),)
    name = "schema-fake"

    def build_command(self, prompt, **kwargs):
        return f"printf %s {shlex.quote(prompt)}"

    def parse_output(self, raw_output):
        return AgentRunResult(result_message=json.loads(raw_output)["result"])

    def build_initial_log_entry(self, prompt, model=None):
        return prompt

    def build_pr_description_command(self, prompt, **kwargs):
        return prompt

    def extract_result_from_json(self, raw_output):
        return raw_output


@pytest.fixture(autouse=True)
def schema_fake(monkeypatch):
    monkeypatch.setitem(_HARNESSES, "schema-fake", SchemaFakeHarness())


def run(coro):
    return asyncio.run(coro)


def script(tmp_path, body):
    path = tmp_path / "flow.py"
    path.write_text('META={"default_harness":"schema-fake"}\n' + body)
    return path


def results(outcome):
    return [
        entry
        for entry in read_journal(outcome.path / "journal.jsonl")
        if entry["type"] == "result"
    ]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a":1}', {"a": 1}),
        ("[1,2]", [1, 2]),
        ('```json\n{"a":1}\n```', {"a": 1}),
        ("```\n[1,2]\n```", [1, 2]),
        ('Before the result: {"a":1}', {"a": 1}),
        ('{"a":1} trailing prose', {"a": 1}),
        ('noise {"nested":{"b":2}} done', {"nested": {"b": 2}}),
        ('{"text":"a } and { inside"}', {"text": "a } and { inside"}),
        ('{"text":"escaped \\" quote"}', {"text": 'escaped " quote'}),
        ('prefix ["{", {"]":"}"}] suffix', ["{", {"]": "}"}]),
        ('unbalanced {"a":1', None),
        ("garbage without json", None),
        ('wrong close {"a":1]', None),
    ],
)
def test_extract_json_corpus(text, expected):
    assert extract_json(text) == expected


def test_strictify_makes_openai_strict_schemas():
    from cacli.flow.schema import strictify

    loose = {
        "type": "object",
        "required": ["a"],
        "properties": {
            "a": {"type": "string"},
            "b": {
                "type": "object",
                "properties": {"c": {"type": "integer"}},
            },
            "items_list": {
                "type": "array",
                "items": {"type": "object", "properties": {"d": {"type": "boolean"}}},
            },
        },
    }
    strict = strictify(loose)
    assert strict["additionalProperties"] is False
    assert strict["required"] == ["a", "b", "items_list"]
    assert strict["properties"]["b"]["additionalProperties"] is False
    assert strict["properties"]["b"]["required"] == ["c"]
    nested = strict["properties"]["items_list"]["items"]
    assert nested["additionalProperties"] is False and nested["required"] == ["d"]
    # original untouched
    assert "additionalProperties" not in loose
    assert loose["required"] == ["a"]
    # explicit additionalProperties preserved
    assert (
        strictify({"type": "object", "additionalProperties": True})[
            "additionalProperties"
        ]
        is True
    )


def test_schema_retry_then_success_and_exhaustion(tmp_path):
    calls = 0

    async def flaky(*args, **kwargs):
        nonlocal calls
        calls += 1
        value = '{"count":"wrong"}' if calls == 1 else '{"count":2}'
        return AsyncExecResult(0, json.dumps({"result": value}))

    source = script(
        tmp_path,
        'async def main():\n    return await agent("count", schema={"type":"object", "required":["count"], "properties":{"count":{"type":"integer"}}}, retries=0)\n',
    )
    outcome = run(
        run_flow(
            source,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": flaky},
        )
    )
    assert outcome.result == {"count": 2}
    journal = read_journal(outcome.path / "journal.jsonl")
    assert len([entry for entry in journal if entry["type"] == "started"]) == 2

    async def invalid(*args, **kwargs):
        return AsyncExecResult(0, json.dumps({"result": '{"count":"still wrong"}'}))

    exhausted = run(
        run_flow(
            source,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": invalid},
        )
    )
    assert exhausted.result is None
    entry = results(exhausted)[0]
    assert entry["error"] == "schema_exhausted"
    assert entry["last_candidate"] == '{"count":"still wrong"}'
    assert (
        len(
            [
                item
                for item in read_journal(exhausted.path / "journal.jsonl")
                if item["type"] == "started"
            ]
        )
        == 3
    )


def test_schema_retries_option_and_retry_prompt(tmp_path):
    commands = []

    async def invalid(command, *args, **kwargs):
        commands.append(command)
        return AsyncExecResult(0, json.dumps({"result": "not json"}))

    source = script(
        tmp_path,
        'async def main():\n    return await agent("original", schema={"type":"object"}, retries=0, schema_retries=1)\n',
    )
    run(
        run_flow(
            source,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": invalid},
        )
    )
    assert len(commands) == 2
    assert "Respond with ONLY a JSON object" in commands[0]
    assert "previous response failed validation" in commands[1]
    assert "not json" in commands[1]


def test_schema_changes_key_decoration_does_not_and_resume_replays_dict(tmp_path):
    calls = []

    async def valid(command, *args, **kwargs):
        calls.append(command)
        return AsyncExecResult(0, json.dumps({"result": '{"value":1}'}))

    schemas = [
        '{"type":"object","properties":{"value":{"type":"integer"}}}',
        '{"type":"object","properties":{"value":{"type":"number"}}}',
    ]
    outcomes = []
    for index, schema in enumerate(schemas):
        source = script(
            tmp_path,
            f'async def main():\n    return await agent("same", schema={schema}, retries=0)\n',
        )
        outcomes.append(
            run(
                run_flow(
                    source,
                    runs_root=tmp_path / "runs",
                    engine_options={"exec_fn": valid},
                )
            )
        )
        if index == 0:
            first_script = tmp_path / "first.py"
            first_script.write_text(source.read_text())
    assert results(outcomes[0])[0]["key"] != results(outcomes[1])[0]["key"]
    assert "Respond with ONLY a JSON object" in calls[0]
    expected_key = JournalKeyChain().next(
        {
            "prompt": "same",
            "harness": "schema-fake",
            "provider": "openai-api",
            "model": None,
            "effort": None,
            "schema": json.loads(schemas[0]),
            "cwd": None,
            "isolation": None,
            "web_search": True,
            "settings": None,
            "sandbox": None,
            "env": None,
            "retries": 0,
            "schema_retries": 2,
            "timeout": 3600.0,
            "stall_timeout": 300.0,
        }
    )
    assert results(outcomes[0])[0]["key"] == expected_key

    async def forbidden(*args, **kwargs):
        raise AssertionError("schema replay must not execute")

    replay = run(
        run_flow(
            first_script,
            resume=outcomes[0].run_id,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": forbidden},
        )
    )
    assert replay.result == {"value": 1}
    assert results(replay)[0]["cached"] is True


@pytest.mark.parametrize(
    ("schema_expr", "message"),
    [
        ('"nope"', "schema must be a dict"),
        ('{"type":"string"}', 'top-level "type"'),
        ('{"type":"object","description":"x"*100001}', "schema is too large"),
    ],
)
def test_schema_boundary_validation(tmp_path, schema_expr, message):
    source = script(
        tmp_path,
        f'async def main():\n    return await agent("x", schema={schema_expr})\n',
    )
    with pytest.raises(FlowRunFailed, match=message):
        run(run_flow(source, runs_root=tmp_path / "runs"))


def test_codex_native_schema_file_is_canonical_and_temporary(tmp_path):
    seen_path = None

    async def codex_result(command, *args, **kwargs):
        nonlocal seen_path
        parts = shlex.split(command)
        seen_path = Path(parts[parts.index("--output-schema") + 1])
        assert seen_path.exists()
        assert (
            seen_path.read_text()
            == '{"additionalProperties":false,"properties":{"answer":{"type":"integer"}},"required":["answer"],"type":"object"}'
        )
        return AsyncExecResult(
            0,
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {"type": "agent_message", "text": '{"answer":42}'},
                }
            ),
        )

    path = tmp_path / "codex-flow.py"
    path.write_text(
        'async def main():\n    return await agent("answer", harness="codex", schema={"type":"object","required":["answer"],"properties":{"answer":{"type":"integer"}}}, retries=0)\n'
    )
    outcome = run(
        run_flow(
            path,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": codex_result},
        )
    )
    assert outcome.result == {"answer": 42}
    assert seen_path is not None and not seen_path.exists()


def test_native_claude_output_is_host_validated_and_retried(tmp_path):
    commands = []

    async def claude_result(command, *args, **kwargs):
        commands.append(command)
        value = {"answer": "wrong"} if len(commands) == 1 else {"answer": 42}
        return AsyncExecResult(
            0,
            json.dumps(
                {
                    "type": "result",
                    "result": "",
                    "structured_output": value,
                    "total_cost_usd": 0.1,
                }
            ),
        )

    path = tmp_path / "claude-flow.py"
    path.write_text(
        'async def main():\n    return await agent("answer", harness="claude", schema={"type":"object","required":["answer"],"properties":{"answer":{"type":"integer"}}}, retries=0)\n'
    )
    outcome = run(
        run_flow(
            path,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": claude_result},
        )
    )
    assert outcome.result == {"answer": 42}
    assert len(commands) == 2
    assert all("--json-schema" in command for command in commands)
    assert "previous response failed validation" in commands[1]
    assert results(outcome)[0]["cost"] == pytest.approx(0.2)
