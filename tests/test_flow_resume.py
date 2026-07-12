import asyncio
import json
import shlex
import sys

import pytest

from cacli.cli import main as cli_main
from cacli.flow import FlowBudgetExceeded, run_flow
from cacli.flow.aexec import AsyncExecResult
from cacli.flow.journal import JournalKeyChain, canonical_json, read_journal
from cacli.flow.runs import show_run
from cacli.providers import _PROVIDERS
from cacli.providers.base import BaseProvider
from cacli.types import AgentRunResult


class FakeProvider(BaseProvider):
    name = "fake"

    def build_command(
        self,
        prompt,
        model=None,
        reasoning_effort=None,
        web_search=True,
        settings=None,
        sandbox=None,
        output_schema=None,
    ):
        prompt = prompt.split("\n\n[Workflow note]", 1)[0]
        payload = json.dumps({"result": f"fake:{prompt}", "cost": 0.25})
        return f"printf %s {shlex.quote(payload)}"

    def parse_output(self, raw_output):
        value = json.loads(raw_output)
        return AgentRunResult(
            result_message=value["result"], total_cost=value.get("cost")
        )

    def build_initial_log_entry(self, prompt, model=None):
        return prompt

    def build_pr_description_command(
        self, prompt, model=None, settings=None, sandbox=None
    ):
        return self.build_command(prompt, model=model)

    def extract_result_from_json(self, raw_output):
        return json.loads(raw_output)["result"]


@pytest.fixture(autouse=True)
def fake_provider(monkeypatch):
    monkeypatch.setitem(_PROVIDERS, "fake", FakeProvider())


def run(coro):
    return asyncio.run(coro)


def write_flow(path, source):
    path.write_text(source)
    return path


def result_entries(outcome):
    return [
        entry
        for entry in read_journal(outcome.path / "journal.jsonl")
        if entry["type"] == "result"
    ]


def successful_exec(cost=0.25, delays=None, calls=None):
    delays = delays or {}

    async def execute(command, *args, **kwargs):
        prompt = next((item for item in delays if item in command), "")
        if calls is not None:
            calls.append(command)
        await asyncio.sleep(delays.get(prompt, 0))
        return AsyncExecResult(0, json.dumps({"result": "ok", "cost": cost}))

    return execute


def test_key_chain_canonical_and_order_sensitive():
    assert canonical_json({"b": 1, "a": 2}) == '{"a":2,"b":1}'
    first = JournalKeyChain()
    second = JournalKeyChain()
    calls = [{"prompt": "a"}, {"prompt": "b"}, {"prompt": "c"}]
    keys = [first.next(call) for call in calls]
    reordered = [second.next(call) for call in [calls[1], calls[0], calls[2]]]
    assert keys != reordered
    assert keys[2] != reordered[2]


def test_parallel_key_assignment_is_completion_order_independent(tmp_path):
    source = """
META={"default_provider":"fake"}
async def main():
    return await parallel([lambda i=i: agent(str(i), retries=0) for i in range(5)])
"""
    script = write_flow(tmp_path / "flow.py", source)
    keys = []
    for delays in (
        {"0": 0.05, "1": 0.04, "2": 0.03, "3": 0.02},
        {"1": 0.01, "3": 0.05},
    ):
        outcome = run(
            run_flow(
                script,
                runs_root=tmp_path / "runs",
                engine_options={"exec_fn": successful_exec(delays=delays)},
            )
        )
        keys.append([entry["key"] for entry in result_entries(outcome)])
    # Journal completion order can differ; index order is the script call order.
    assert sorted(keys[0]) == sorted(keys[1])
    by_index = []
    for run_id in (tmp_path / "runs").glob("fl_*"):
        entries = [
            e for e in read_journal(run_id / "journal.jsonl") if e["type"] == "result"
        ]
        by_index.append([e["key"] for e in sorted(entries, key=lambda e: e["index"])])
    assert by_index[0] == by_index[1]


def test_labels_phases_and_engine_note_do_not_change_keys(tmp_path, monkeypatch):
    import cacli.flow.engine as engine_module

    scripts = [
        """
META={"default_provider":"fake"}
async def main():
    phase("one")
    return await agent("same prompt", label="old", retries=0)
""",
        """
META={"default_provider":"fake"}
async def main():
    phase("two")
    return await agent("same prompt", label="new", phase="three", retries=0)
""",
    ]
    keys = []
    for index, source in enumerate(scripts):
        monkeypatch.setattr(engine_module, "FLOW_PROMPT_SUFFIX", f"\nNOTE {index}")
        outcome = run(
            run_flow(
                write_flow(tmp_path / f"flow{index}.py", source),
                runs_root=tmp_path / "runs",
                engine_options={"exec_fn": successful_exec()},
            )
        )
        keys.append(result_entries(outcome)[0]["key"])
    assert keys[0] == keys[1]


@pytest.mark.parametrize(
    ("old_option", "new_option"),
    [
        ("retries=0", "retries=1"),
        ("sandbox=False", "sandbox=True"),
        ("web_search=True", "web_search=False"),
        ('env={"MODE":"old"}', 'env={"MODE":"new"}'),
    ],
)
def test_execution_options_bust_resume_cache(tmp_path, old_option, new_option):
    def source(option):
        return f"""\
META={{"default_provider":"fake"}}
async def main():
    return await agent("same prompt", {option})
"""

    script = write_flow(tmp_path / "flow.py", source(old_option))
    original = run(
        run_flow(
            script,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec()},
        )
    )
    calls = []
    script.write_text(source(new_option))
    resumed = run(
        run_flow(
            script,
            resume=original.run_id,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec(calls=calls)},
        )
    )

    old_entry = result_entries(original)[0]
    new_entry = result_entries(resumed)[0]
    assert old_entry["key"] != new_entry["key"]
    assert len(calls) == 1
    assert new_entry.get("cached") is None


def test_relative_cwd_keys_are_checkout_portable(tmp_path):
    source = """
META={"default_provider":"fake"}
async def main():
    return await agent("same", cwd="sub", retries=0)
"""
    keys = []
    for name in ("checkout-a", "checkout-b"):
        root = tmp_path / name
        root.mkdir()
        (root / "sub").mkdir()
        outcome = run(
            run_flow(
                write_flow(root / "flow.py", source),
                cwd=root,
                runs_root=tmp_path / "runs",
                engine_options={"exec_fn": successful_exec()},
            )
        )
        keys.append(result_entries(outcome)[0]["key"])
    assert keys[0] == keys[1]


def test_full_resume_replays_without_exec_and_records_meta(tmp_path):
    source = """
META={"default_provider":"fake"}
async def main():
    return [await agent("a", retries=0), await agent("b", retries=0)]
"""
    script = write_flow(tmp_path / "flow.py", source)
    original = run(
        run_flow(
            script,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec()},
        )
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("exec must not run during full replay")

    resumed = run(
        run_flow(
            script,
            resume=original.run_id,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": forbidden},
        )
    )
    shown = show_run(resumed.run_id, tmp_path / "runs")
    assert resumed.result == original.result
    assert shown["meta"]["resumed_from"] == original.run_id
    assert all(entry["cached"] for entry in result_entries(resumed))
    assert shown["meta"]["totals"]["replayed_cost"] == pytest.approx(0.5)
    assert [e["type"] for e in shown["events"]].count("cached") == 2


def test_resume_replays_prefix_then_permanently_diverges(tmp_path):
    original_source = """
META={"default_provider":"fake"}
async def main():
    return [await agent("a", retries=0), await agent("b", retries=0), await agent("c", retries=0)]
"""
    script = write_flow(tmp_path / "flow.py", original_source)
    original = run(
        run_flow(
            script,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec()},
        )
    )
    script.write_text(original_source.replace('agent("b"', 'agent("edited"'))
    calls = []
    resumed = run(
        run_flow(
            script,
            resume=original.run_id,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec(calls=calls)},
        )
    )
    entries = sorted(result_entries(resumed), key=lambda entry: entry["index"])
    assert [entry.get("cached", False) for entry in entries] == [True, False, False]
    assert len(calls) == 2
    assert (
        len(
            [
                e
                for e in read_journal(resumed.path / "events.jsonl")
                if e["type"] == "diverged"
            ]
        )
        == 1
    )


def test_cached_terminal_none_replays_without_rerun(tmp_path):
    script = write_flow(
        tmp_path / "flow.py",
        'META={"default_provider":"fake"}\nasync def main():\n    return await agent("x", retries=0)',
    )

    async def fail(*args, **kwargs):
        return AsyncExecResult(1, "")

    original = run(
        run_flow(
            script,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": fail},
        )
    )

    async def forbidden(*args, **kwargs):
        raise AssertionError("cached None must be terminal")

    resumed = run(
        run_flow(
            script,
            resume=original.run_id,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": forbidden},
        )
    )
    assert resumed.result is None
    assert result_entries(resumed)[0]["cached"] is True


def test_incomplete_started_entry_respawns(tmp_path):
    script = write_flow(
        tmp_path / "flow.py",
        'META={"default_provider":"fake"}\nasync def main():\n    return await agent("x", retries=0)',
    )
    original = run(
        run_flow(
            script,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec()},
        )
    )
    journal_path = original.path / "journal.jsonl"
    entries = read_journal(journal_path)
    journal_path.write_text(
        "\n".join(json.dumps(e) for e in entries if e["type"] == "started") + "\n"
    )
    calls = []
    resumed = run(
        run_flow(
            script,
            resume=original.run_id,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec(calls=calls)},
        )
    )
    assert len(calls) == 1
    logs = [
        e for e in read_journal(resumed.path / "events.jsonl") if e["type"] == "log"
    ]
    assert "respawning incomplete agent" in logs[0]["message"]


def test_estimated_cost_and_sandbox_budget_functions(tmp_path):
    source = """
META={"default_provider":"fake", "budget":0.5}
async def main():
    before = [budget_total(), budget_spent(), budget_remaining()]
    await agent("x", retries=0)
    return before + [budget_spent(), budget_remaining()]
"""

    async def no_cost(*args, **kwargs):
        return AsyncExecResult(0, '{"result":"ok"}')

    outcome = run(
        run_flow(
            write_flow(tmp_path / "flow.py", source),
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": no_cost},
        )
    )
    assert outcome.result == [0.5, 0.0, 0.5, 0.1, 0.4]
    entry = result_entries(outcome)[0]
    assert entry["cost"] == 0.1
    assert entry["cost_estimated"] is True
    assert (
        json.loads((outcome.path / "meta.json").read_text())["totals"]["cost_estimated"]
        == 0.1
    )


def test_unset_budget_remaining_is_positive_infinity(tmp_path):
    source = """
async def main():
    return [budget_total() is None, budget_spent(), budget_remaining() > 1e300]
"""
    outcome = run(
        run_flow(
            write_flow(tmp_path / "flow.py", source),
            provider="fake",
            runs_root=tmp_path / "runs",
        )
    )
    assert outcome.result == [True, 0.0, True]


def test_budget_trips_on_next_call_and_resume_with_higher_budget(tmp_path):
    source = """
META={"default_provider":"fake"}
async def main():
    return [await agent("a", retries=0), await agent("b", retries=0)]
"""
    script = write_flow(tmp_path / "flow.py", source)
    with pytest.raises(FlowBudgetExceeded) as caught:
        run(
            run_flow(
                script,
                budget=0.20,
                runs_root=tmp_path / "runs",
                engine_options={"exec_fn": successful_exec()},
            )
        )
    failed_id = caught.value.run_id
    failed = show_run(failed_id, tmp_path / "runs")
    assert failed["meta"]["status"] == "budget_exceeded"
    assert len([e for e in failed["journal"] if e["type"] == "result"]) == 1

    calls = []
    resumed = run(
        run_flow(
            script,
            budget=0.50,
            resume=failed_id,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec(calls=calls)},
        )
    )
    assert resumed.status == "completed"
    assert len(calls) == 1
    entries = sorted(result_entries(resumed), key=lambda entry: entry["index"])
    assert entries[0]["cached"] is True
    assert entries[1].get("cached") is None
    totals = json.loads((resumed.path / "meta.json").read_text())["totals"]
    assert totals["replayed_cost"] == 0.25
    assert totals["cost"] == 0.5


def test_parallel_inflight_results_survive_budget_overshoot(tmp_path):
    source = """
META={"default_provider":"fake"}
async def main():
    await parallel([lambda: agent("a", retries=0), lambda: agent("b", retries=0)])
    return await agent("after", retries=0)
"""
    with pytest.raises(FlowBudgetExceeded) as caught:
        run(
            run_flow(
                write_flow(tmp_path / "flow.py", source),
                budget=0.20,
                runs_root=tmp_path / "runs",
                engine_options={"exec_fn": successful_exec(delays={"a": 0.02})},
            )
        )
    shown = show_run(caught.value.run_id, tmp_path / "runs")
    assert len([e for e in shown["journal"] if e["type"] == "result"]) == 2
    assert shown["meta"]["totals"]["cost"] == pytest.approx(0.5)


def test_queued_parallel_agents_recheck_budget_before_exec(tmp_path):
    source = """
META={"default_provider":"fake", "concurrency":1}
async def main():
    return await parallel([
        lambda i=i: agent(str(i), retries=0) for i in range(5)
    ])
"""
    calls = []
    with pytest.raises(FlowBudgetExceeded) as caught:
        run(
            run_flow(
                write_flow(tmp_path / "flow.py", source),
                budget=0.20,
                runs_root=tmp_path / "runs",
                engine_options={"exec_fn": successful_exec(calls=calls)},
            )
        )

    shown = show_run(caught.value.run_id, tmp_path / "runs")
    assert len(calls) == 1
    assert shown["meta"]["totals"]["cost"] == pytest.approx(0.25)
    assert len([e for e in shown["journal"] if e["type"] == "result"]) == 1


def test_replayed_cost_counts_toward_budget(tmp_path):
    script = write_flow(
        tmp_path / "flow.py",
        """
META={"default_provider":"fake"}
async def main():
    return [await agent("a", retries=0), await agent("b", retries=0)]
""",
    )
    original = run(
        run_flow(
            script,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": successful_exec()},
        )
    )
    with pytest.raises(FlowBudgetExceeded) as caught:
        run(
            run_flow(
                script,
                resume=original.run_id,
                budget=0.20,
                runs_root=tmp_path / "runs",
                engine_options={"exec_fn": successful_exec()},
            )
        )
    totals = show_run(caught.value.run_id, tmp_path / "runs")["meta"]["totals"]
    assert totals["replayed_cost"] == 0.25
    assert totals["cost"] == 0.25


def test_cli_warns_when_resume_args_differ(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    script = write_flow(
        tmp_path / "flow.py",
        'async def main():\n    return await agent("same", provider="fake", retries=0)',
    )
    original = run(run_flow(script, args={"value": 1}))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cacli",
            "flow",
            "run",
            str(script),
            "--args",
            '{"value":2}',
            "--resume",
            original.run_id,
        ],
    )
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
    assert "args differ from original run" in capsys.readouterr().err


def test_cli_budget_exceeded_then_resume_higher_from_cached_prefix(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    script = write_flow(
        tmp_path / "flow.py",
        """
async def main():
    return [
        await agent("first", provider="fake", retries=0),
        await agent("second", provider="fake", retries=0),
    ]
""",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["cacli", "flow", "run", str(script), "--budget", "0.20"],
    )
    with pytest.raises(SystemExit) as first_exit:
        cli_main()
    assert first_exit.value.code == 1
    capsys.readouterr()
    runs_dir = home / ".cacli" / "flows"
    failed_meta = next(runs_dir.glob("fl_*/meta.json"))
    failed = json.loads(failed_meta.read_text())
    assert failed["status"] == "budget_exceeded"

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cacli",
            "flow",
            "run",
            str(script),
            "--budget",
            "0.50",
            "--resume",
            failed["id"],
        ],
    )
    with pytest.raises(SystemExit) as resumed_exit:
        cli_main()
    assert resumed_exit.value.code == 0
    output = capsys.readouterr().out
    assert "cached  first [fake]" in output
    assert "fake:second" in output
    completed = [
        json.loads(path.read_text())
        for path in runs_dir.glob("fl_*/meta.json")
        if path != failed_meta
    ]
    assert completed[0]["status"] == "completed"
    assert completed[0]["resumed_from"] == failed["id"]
