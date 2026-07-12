"""Phase-0 spike: prove pydantic-monty 0.0.18 supports the cacli flow engine design.

0.0.18 has no class definitions, so the DSL is flat module-level functions injected
by a trusted shim: agent(), parallel(), pipeline(), phase(), log(), flow_args.

Checks:
1. async external function round-trip (sandboxed await -> host coroutine -> result)
2. real concurrency: N gather branches -> N overlapping host coroutines
3. shim-side parallel/pipeline consuming sandbox lambdas
4. lambdas with default-arg capture, closures, comprehensions, f-strings, zip
5. call-order determinism of host-callback arrival (resume-key foundation)
6. exception translation: host exception -> catchable in sandbox
"""

import asyncio
import time

from pydantic_monty import Monty

USER_SCRIPT = """
META = {"name": "spike", "phases": ["Find", "Verify"]}

FINDINGS = {"type": "object"}

async def main():
    phase("Find")
    specs = [("find bugs", "codex"), ("find security issues", "claude"), ("find perf issues", "gemini")]
    rounds = await parallel([
        lambda p=p, prov=prov: agent(p, provider=prov, schema=FINDINGS)
        for p, prov in specs
    ])
    findings = [f for r in rounds if r for f in r["findings"]]

    verified = await pipeline(
        findings,
        lambda f, *_: agent(f"refute: {f['summary']}", provider="codex"),
    )
    confirmed = [f for f, v in zip(findings, verified) if v is not None]

    phase("Verify")
    log(f"confirmed {len(confirmed)} findings, args={flow_args}")

    caught = None
    try:
        await agent("EXPLODE", provider="codex")
    except Exception as exc:
        caught = f"caught: {exc}"

    return {"confirmed": confirmed, "caught": caught}
"""

SHIM_PRELUDE = """
import asyncio

flow_args = _ctx_args

def phase(title):
    return _ctx_phase(title)

def log(message):
    return _ctx_log(message)

async def agent(prompt, provider="claude", model=None, effort=None, schema=None, label=None, phase=None):
    return await _ctx_agent(prompt, provider=provider, model=model, effort=effort,
                            schema=schema, label=label, phase=phase)

async def parallel(thunks):
    async def run_one(thunk):
        try:
            return await thunk()
        except Exception as exc:
            _ctx_log(f"parallel branch failed: {exc}")
            return None
    return await asyncio.gather(*[run_one(t) for t in thunks])

async def pipeline(items, stage):
    async def run_one(item, index):
        try:
            return await stage(item, item, index)
        except Exception as exc:
            _ctx_log(f"pipeline item {index} failed: {exc}")
            return None
    return await asyncio.gather(*[run_one(item, i) for i, item in enumerate(items)])
"""

SHIM_EPILOGUE = """
_result = asyncio.run(main())
_result
"""


class Host:
    def __init__(self):
        self.calls = []
        self.overlap_window = []
        self.logs = []
        self.phases = []

    async def agent(
        self,
        prompt,
        provider="claude",
        model=None,
        effort=None,
        schema=None,
        label=None,
        phase=None,
    ):
        self.calls.append((prompt, provider))
        if prompt == "EXPLODE":
            raise RuntimeError("provider exploded")
        start = time.monotonic()
        await asyncio.sleep(0.2)  # stand-in for a subprocess
        self.overlap_window.append((start, time.monotonic()))
        if schema is not None:
            return {"findings": [{"summary": f"{provider} finding for: {prompt}"}]}
        return f"{provider} says ok: {prompt}"

    def log(self, message):
        self.logs.append(message)

    def phase(self, title):
        self.phases.append(title)


async def run_flow():
    host = Host()
    m = Monty(
        SHIM_PRELUDE + USER_SCRIPT + SHIM_EPILOGUE,
        inputs=["_ctx_args"],
        script_name="spike_flow.py",
    )
    result = await m.run_async(
        inputs={"_ctx_args": {"pr": 42}},
        external_functions={
            "_ctx_agent": host.agent,
            "_ctx_log": host.log,
            "_ctx_phase": host.phase,
        },
        limits={"max_duration_secs": 5.0, "max_memory": 64 * 1024 * 1024},
    )
    return host, result


def main():
    print("== flow run ==")
    host, result = asyncio.run(run_flow())
    print("result:", result)
    print("phases:", host.phases)
    print("logs:", host.logs)
    print("call order:", [(p[:25], prov) for p, prov in host.calls])

    assert result["caught"] and "provider exploded" in result["caught"], result
    assert len(result["confirmed"]) == 3, result
    assert host.phases == ["Find", "Verify"]

    windows = host.overlap_window[:3]
    starts = [s for s, _ in windows]
    ends = [e for _, e in windows]
    assert max(starts) < min(ends), f"finders did not overlap: {windows}"
    print(
        f"finder concurrency confirmed (start spread {max(starts)-min(starts):.3f}s, each sleeps 0.2s)"
    )

    order0 = host.calls
    for _ in range(3):
        h, _ = asyncio.run(run_flow())
        assert (
            h.calls == order0
        ), f"nondeterministic call order:\n{order0}\nvs\n{h.calls}"
    print("call-order determinism confirmed across 4 runs")

    print("\nSPIKE PASSED")


if __name__ == "__main__":
    main()
