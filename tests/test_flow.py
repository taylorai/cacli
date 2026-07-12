import asyncio
import json
import os
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from cacli.cli import main as cli_main
from cacli.flow import FlowCancelled, FlowRunFailed, run_flow
from cacli.flow.aexec import AsyncExecResult, aexec
from cacli.flow.host import FlowHost
from cacli.flow.runs import ResultEncodingError, normalize_json, show_run
from cacli.flow.sandbox import FlowScriptError, execute, extract_meta
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
        if prompt == "fail":
            return "sh -c 'exit 7'"
        if prompt == "garbage":
            return "printf 'not-json'"
        delay = 2 if prompt == "slow" else 0
        payload = json.dumps({"result": f"fake:{prompt}", "cost": 0.25})
        return (
            f"sh -c {shlex.quote(f'sleep {delay}; printf %s {shlex.quote(payload)}')}"
        )

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


def write_flow(tmp_path, source):
    path = tmp_path / "flow.py"
    path.write_text(source)
    return path


def run(coro):
    return asyncio.run(coro)


def test_meta_literal_extraction_does_not_execute():
    source = 'META = {"name": "safe", "concurrency": 3}\nraise RuntimeError()'
    assert extract_meta(source) == {"name": "safe", "concurrency": 3}
    with pytest.raises(FlowScriptError, match="literal dict"):
        extract_meta("META = dict(name='unsafe')")


def test_worktree_isolation_error_is_user_facing(tmp_path):
    engine = SimpleNamespace(cancel_event=asyncio.Event())
    host = FlowHost(engine, cwd=tmp_path, default_provider="fake")
    with pytest.raises(ValueError, match="^worktree isolation is not supported$"):
        run(host._ctx_agent("test", isolation="worktree"))


class RecordingHost:
    def __init__(self):
        self.calls = []
        self.logs = []
        self.phases = []

    async def _ctx_agent(self, prompt, **kwargs):
        self.calls.append(("start", prompt, time.monotonic()))
        if prompt == "explode":
            raise RuntimeError("boom")
        if prompt == "cancel":
            raise RuntimeError("FlowCancelled: stop")
        await asyncio.sleep(0.15 if "slow" in prompt else 0.01)
        self.calls.append(("end", prompt, time.monotonic()))
        return prompt + ":result"

    def _ctx_log(self, message):
        self.logs.append(message)

    def _ctx_phase(self, title):
        self.phases.append(title)

    def _ctx_budget_total(self):
        return None

    def _ctx_budget_spent(self):
        return 0.0

    def _ctx_budget_remaining(self):
        return float("inf")


def test_sandbox_dsl_conformance_fanout_pipeline_and_closures():
    source = """
META = {"name": "conformance"}

async def main():
    phase("Find")
    prefix = "item"
    schema = {"type": "object"}
    values = await parallel([
        lambda i=i: agent(f"{prefix}-{i}") for i in range(3)
    ])
    async def stage1(previous, original, index):
        return await agent(
            ("slow-" if index == 0 else "fast-") + str(index), schema=schema
        )
    async def stage2(previous, original, index):
        return await agent("stage2-" + str(index) + "-" + previous, schema=None)
    piped = await pipeline(values, stage1, stage2)
    caught = None
    try:
        await agent("explode")
    except Exception as exc:
        caught = str(exc)
    log(f"args={flow_args}")
    return {"piped": piped, "caught": caught}
"""
    host = RecordingHost()
    result = run(execute(source, "flow.py", {"pr": 1}, host))
    assert len(result["piped"]) == 3
    assert result["caught"] == "boom"
    assert host.phases == ["Find"]
    assert "{'pr': 1}" in host.logs[-1]
    order = [(kind, prompt) for kind, prompt, _ in host.calls]
    assert order.index(("start", "stage2-1-fast-1:result")) < order.index(
        ("end", "slow-0")
    )


def test_parallel_nulls_ordinary_failure_and_propagates_cancel():
    host = RecordingHost()
    source = """
async def explode_locally():
    raise ValueError("boom")
async def main():
    return await parallel([lambda: explode_locally(), lambda: agent("ok")])
"""
    assert run(execute(source, "flow.py", None, host))[0] is None
    with pytest.raises(FlowScriptError, match="FlowCancelled"):
        run(
            execute(
                'async def main():\n    return await parallel([lambda: agent("cancel")])',
                "flow.py",
                None,
                RecordingHost(),
            )
        )


def test_deferred_await_agents_run_concurrently():
    # agent is an eager external-function alias (JS-promise style): calling it
    # starts the host coroutine immediately, so t1/t2 overlap without parallel().
    source = """
async def main():
    t1 = agent("slow-one")
    t2 = agent("slow-two")
    return [await t1, await t2]
"""
    host = RecordingHost()
    started = time.monotonic()
    result = run(execute(source, "flow.py", None, host))
    elapsed = time.monotonic() - started
    assert result == ["slow-one:result", "slow-two:result"]
    starts = [t for kind, _, t in host.calls if kind == "start"]
    ends = [t for kind, _, t in host.calls if kind == "end"]
    assert max(starts) < min(ends), "agent calls did not overlap"
    assert elapsed < 0.28, f"deferred-await ran sequentially ({elapsed:.2f}s)"


def test_unawaited_eager_agents_are_drained_before_completion(tmp_path):
    source = """
META = {"default_provider": "fake", "concurrency": 1}
async def main():
    agent("orphan-one", retries=0)
    agent("orphan-two", retries=0)
    agent("orphan-three", retries=0)
    return "done"
"""
    calls = []

    async def delayed_exec(command, *args, **kwargs):
        calls.append(command)
        await asyncio.sleep(0.1)
        return AsyncExecResult(0, '{"result":"late","cost":0.25}')

    outcome = run(
        run_flow(
            write_flow(tmp_path, source),
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": delayed_exec},
        )
    )
    meta_path = outcome.path / "meta.json"
    journal_path = outcome.path / "journal.jsonl"
    completed_meta = meta_path.read_text()
    completed_journal = journal_path.read_text()
    completed_calls = len(calls)
    time.sleep(0.2)

    assert outcome.status == "completed"
    assert outcome.result == "done"
    assert meta_path.read_text() == completed_meta
    assert journal_path.read_text() == completed_journal
    assert len(calls) == completed_calls
    assert '"type":"result"' not in completed_journal


def test_effective_status_detects_dead_pid():
    from cacli.flow.runs import effective_status

    assert effective_status({"status": "completed", "pid": 1}) == "completed"
    assert effective_status({"status": "running"}) == "running"
    assert effective_status({"status": "running", "pid": os.getpid()}) == "running"
    dead = subprocess.Popen(["true"])
    dead.wait()
    assert effective_status({"status": "running", "pid": dead.pid}) == "interrupted"


def test_duration_limit_pauses_during_host_await():
    class SlowHost(RecordingHost):
        async def _ctx_agent(self, prompt, **kwargs):
            await asyncio.sleep(1.1)
            return "ok"

    source = 'async def main():\n    return await agent("sleep")'
    started = time.monotonic()
    assert (
        run(
            execute(
                source,
                "flow.py",
                None,
                SlowHost(),
                limits={"max_duration_secs": 0.5},
            )
        )
        == "ok"
    )
    assert time.monotonic() - started >= 1.0


@pytest.mark.parametrize(
    "body,pattern",
    [
        ('return open("/etc/passwd").read()', "not implemented"),
        ('return __import__("os")', "__import__"),
        ("import socket\n    return None", "socket"),
        ("import os\n    return os.environ", "not implemented"),
        ("while True:\n        pass", "time limit exceeded"),
    ],
)
def test_sandbox_escape_smoke(body, pattern):
    source = f"async def main():\n    {body}"
    with pytest.raises(FlowScriptError, match=pattern):
        run(
            execute(
                source,
                "flow.py",
                None,
                RecordingHost(),
                limits={"max_duration_secs": 0.02},
            )
        )


def test_result_json_normalization():
    assert normalize_json((1, {"x": (2,)})) == [1, {"x": [2]}]
    with pytest.raises(ResultEncodingError, match="set"):
        normalize_json({1, 2})
    with pytest.raises(ResultEncodingError, match="bytes"):
        normalize_json(b"x")


def test_aexec_incremental_capture_stall_and_env_hygiene(tmp_path):
    chunks = []

    async def scenario():
        captured = await aexec(
            "printf first; sleep 0.03; printf second:${CLAUDECODE-unset}",
            str(tmp_path),
            2,
            {"CLAUDECODE": "nested"},
            on_output=chunks.append,
            stall_timeout=1,
        )
        stalled = await aexec("sleep 1", str(tmp_path), 2, stall_timeout=0.03)
        return captured, stalled

    captured, stalled = run(scenario())
    assert captured.stdout == "firstsecond:unset"
    assert "".join(chunks) == captured.stdout
    assert captured.last_output_at is not None
    assert stalled.stalled is True
    assert stalled.exit_code == 124


def test_aexec_kills_term_ignoring_pipe_holder(tmp_path, monkeypatch):
    import cacli.flow.aexec as aexec_module

    monkeypatch.setattr(aexec_module, "READER_DRAIN_TIMEOUT", 0.1)
    process = None

    def record_process(value):
        nonlocal process
        process = value

    child = shlex.quote(
        str(sys.executable)
        + " -c 'import signal,time; "
        + "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)'"
    )
    started = time.monotonic()
    result = run(
        aexec(
            f"sh -c {child} & wait",
            str(tmp_path),
            2,
            stall_timeout=0.03,
            on_process=record_process,
        )
    )
    elapsed = time.monotonic() - started

    assert result.stalled is True
    assert elapsed < 3
    assert process is not None
    with pytest.raises(ProcessLookupError):
        os.killpg(process.pid, 0)


def test_aexec_capture_keeps_bounded_tail(tmp_path, monkeypatch):
    import cacli.flow.aexec as aexec_module

    monkeypatch.setattr(aexec_module, "MAX_CAPTURE_BYTES", 8)
    large = run(aexec("printf abcdefghijkl", str(tmp_path), 2))
    small = run(aexec("printf small", str(tmp_path), 2))

    assert large.stdout == "efghijkl"
    assert len(large.stdout.encode()) <= 8
    assert large.stdout_truncated is True
    assert small.stdout == "small"
    assert small.stdout_truncated is False


def test_engine_success_failure_retries_timeout_and_journal(tmp_path):
    source = """
META = {"default_provider": "fake"}
async def main():
    return [
        await agent("hello", retries=0),
        await agent("fail", retries=1),
        await agent("garbage", retries=1),
        await agent("slow", timeout=0.1, stall_timeout=1, retries=0),
    ]
"""
    outcome = run(
        run_flow(
            write_flow(tmp_path, source),
            runs_root=tmp_path / "runs",
            engine_options={"retry_delays": (0.0,), "sleep": asyncio.sleep},
        )
    )
    assert outcome.result == ["fake:hello", None, None, None]
    shown = show_run(outcome.run_id, tmp_path / "runs")
    assert shown["meta"]["totals"] == {
        "agents": 4,
        "succeeded": 1,
        "failed": 3,
        "cost": 0.25,
        "cost_estimated": 0.0,
        "budget": None,
        "replayed_cost": 0.0,
    }
    results = [e for e in shown["journal"] if e["type"] == "result"]
    started = [e for e in shown["journal"] if e["type"] == "started"]
    assert len(results) == 4
    assert len(started) == 6
    assert all(e["key"] and e["cost_estimated"] is False for e in results)
    assert shown["meta"]["status"] == "completed"


def test_retry_can_recover_parseable_output(tmp_path):
    attempts = 0

    async def flaky_exec(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return AsyncExecResult(1, "")
        return AsyncExecResult(0, '{"result":"recovered","cost":0.5}')

    source = 'META={"default_provider":"fake"}\nasync def main():\n    return await agent("x")'
    outcome = run(
        run_flow(
            write_flow(tmp_path, source),
            runs_root=tmp_path / "runs",
            engine_options={
                "exec_fn": flaky_exec,
                "retry_delays": (0.0,),
                "sleep": asyncio.sleep,
            },
        )
    )
    assert outcome.result == "recovered"
    assert attempts == 2


def test_semaphore_bounds_concurrent_agents(tmp_path):
    active = 0
    maximum = 0

    async def tracking_exec(*args, **kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        await asyncio.sleep(0.03)
        active -= 1
        return AsyncExecResult(0, '{"result":"ok"}')

    source = """
META={"default_provider":"fake"}
async def main():
    return await parallel([lambda i=i: agent(str(i), retries=0) for i in range(8)])
"""
    run(
        run_flow(
            write_flow(tmp_path, source),
            concurrency=3,
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": tracking_exec},
        )
    )
    assert maximum == 3


def test_agent_cap_fails_run(tmp_path):
    source = """
META={"default_provider":"fake", "max_agents":1}
async def main():
    await agent("one", retries=0)
    return await agent("two", retries=0)
"""
    with pytest.raises(FlowRunFailed, match="agent lifetime cap"):
        run(run_flow(write_flow(tmp_path, source), runs_root=tmp_path / "runs"))


def test_cancellation_terminates_live_agent(tmp_path):
    source = """
META={"default_provider":"fake"}
async def main():
    try:
        return await agent("slow", timeout=30, retries=0)
    finally:
        log("finally ran")
"""
    cancel = asyncio.Event()

    async def scenario():
        task = asyncio.create_task(
            run_flow(
                write_flow(tmp_path, source),
                runs_root=tmp_path / "runs",
                cancel_event=cancel,
            )
        )
        await asyncio.sleep(0.15)
        cancel.set()
        with pytest.raises(FlowCancelled):
            await asyncio.wait_for(task, 3)

    run(scenario())
    meta = next((tmp_path / "runs").glob("fl_*/meta.json"))
    assert json.loads(meta.read_text())["status"] == "cancelled"
    events = next((tmp_path / "runs").glob("fl_*/events.jsonl")).read_text()
    assert "finally ran" in events


def test_schema_round_trip_returns_mapping_to_sandbox(tmp_path):
    source = """
META={"default_provider":"fake"}
async def main():
    r = await agent("x", schema={"type":"object", "required":["findings"]})
    return r["findings"]
"""

    async def structured(*args, **kwargs):
        return AsyncExecResult(
            0, json.dumps({"result": 'Here: ```json\\n{"findings":["one"]}\\n```'})
        )

    outcome = run(
        run_flow(
            write_flow(tmp_path, source),
            runs_root=tmp_path / "runs",
            engine_options={"exec_fn": structured},
        )
    )
    assert outcome.result == ["one"]


def test_cli_flow_run_end_to_end_with_fake_provider(tmp_path, monkeypatch, capsys):
    source = """
async def main():
    return {"answer": await agent("cli", retries=0)}
"""
    script = write_flow(tmp_path, source)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        sys,
        "argv",
        ["cacli", "flow", "run", str(script), "--provider", "fake"],
    )
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
    output = capsys.readouterr().out
    assert "fake:cli" in output
    assert "queued" in output and "done" in output
    assert next((home / ".cacli" / "flows").glob("fl_*/result.json"))


def test_cli_flow_install_skill_installs_then_updates(tmp_path, monkeypatch, capsys):
    argv = ["cacli", "flow", "install-skill", "--dir", str(tmp_path)]
    monkeypatch.setattr(sys, "argv", argv)
    cli_main()
    skill_path = tmp_path / "cacli-flow" / "SKILL.md"
    assert skill_path.is_file()
    assert "cacli flow" in skill_path.read_text()
    assert "installed" in capsys.readouterr().out

    monkeypatch.setattr(sys, "argv", argv)
    cli_main()
    assert "updated" in capsys.readouterr().out


def test_cli_flow_docs_omits_frontmatter(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cacli", "flow", "docs"])
    cli_main()
    output = capsys.readouterr().out
    assert output.splitlines()[0] != "---"
    assert "# cacli flow: cross-provider agent workflows" in output


def test_cli_flow_run_help_mentions_flow_args(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cacli", "flow", "run", "-h"])
    with pytest.raises(SystemExit) as exc:
        cli_main()
    assert exc.value.code == 0
    assert "flow_args" in capsys.readouterr().out
