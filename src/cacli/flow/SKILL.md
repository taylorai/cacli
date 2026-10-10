---
name: cacli-flow
description: Orchestrate multiple coding agents (claude, codex, devin, cursor, opencode, pi) from one sandboxed Python workflow script via `cacli flow`. Use when a task calls for fanning out agent calls in parallel, cross-vendor review/verification panels, racing providers, structured JSON output from agents, budget-capped agent loops, or resumable multi-agent pipelines. Triggers: "workflow", "fan out", "run agents in parallel", "cross-review", "judge panel", "cacli flow".
---

# cacli flow: cross-harness agent workflows

`cacli flow run <script.py>` executes a Python-flavored workflow script that
orchestrates coding-agent subprocesses across harnesses and inference providers. The script runs in a
locked-down interpreter sandbox (Monty): no imports beyond a fixed builtin set,
no filesystem/network/env/subprocess access. All side effects go through the
injected functions below — the agents you spawn run with normal permissions;
only the orchestration script is boxed.

## Quick start

```python
# review_flow.py
META = {"name": "cross-review", "description": "cross-vendor review"}

FINDINGS = {
    "type": "object",
    "required": ["findings"],
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["file", "summary"],
                "properties": {
                    "file": {"type": "string"},
                    "summary": {"type": "string"},
                },
            },
        }
    },
}


async def main():
    phase("Find")
    rounds = await parallel(
        [
            lambda p=p, prov=prov: agent(p, harness=prov, schema=FINDINGS)
            for p, prov in [
                ("find correctness bugs in the uncommitted diff", "codex"),
                ("find security issues in the uncommitted diff", "claude"),
            ]
        ]
    )
    findings = [f for r in rounds if r for f in r["findings"]]

    phase("Verify")
    verdicts = await pipeline(
        findings,
        lambda f, *_: agent(
            f"Try to REFUTE this finding, answer refuted true/false: {f}",
            harness="codex",
            schema={
                "type": "object",
                "required": ["refuted"],
                "properties": {"refuted": {"type": "boolean"}},
            },
        ),
    )
    confirmed = [f for f, v in zip(findings, verdicts) if v and not v["refuted"]]
    log(f"{len(confirmed)} findings confirmed")
    return {"confirmed": confirmed}
```

```bash
cacli flow run review_flow.py --budget 5.00
cacli flow run review_flow.py --args '{"pr": 42}'   # available as flow_args
```

## Script contract

- Define `META = {...}` as a **literal dict** at top level (extracted without
  executing the script). Keys: `name`, `description`, optional `phases` (display
  only), `default_harness`, `default_provider`, `budget`, `concurrency`, `max_agents`.
- Define `async def main():`. Its return value must be JSON-encodable (tuples
  become lists; sets/bytes are rejected) and is written to `result.json`.
- Everything else is plain Python within the sandbox subset (below).

## Injected API

### `agent(prompt, **kwargs) -> str | dict | list | None`

Spawns one harness CLI subprocess. **Eager**: the agent starts the moment you
call `agent(...)` — awaiting later just collects the result, so
`t1 = agent(...); t2 = agent(...); await t1; await t2` runs both concurrently.
All arguments after `prompt` are keyword-only.

| kwarg | default | meaning |
|---|---|---|
| `harness` | flow default (`--harness` / `META["default_harness"]` / `"claude"`) | `"claude"`, `"codex"`, `"devin"`, `"cursor"`, `"opencode"`, `"pi"` |
| `provider` | harness default (or `--provider` / `META["default_provider"]`) | inference endpoint and account; supported choices depend on the harness |
| `model` | provider default | provider model aliases accepted |
| `effort` | None | `"low"`/`"medium"`/`"high"`/`"xhigh"`/`"max"` where supported |
| `schema` | None | JSON Schema dict (top-level `type` must be `"object"` or `"array"`, ≤100KB); returns a validated dict/list instead of str (see Structured output) |
| `label` | first 60 chars of prompt | display label (not part of resume identity) |
| `phase` | ambient `phase()` value | explicit phase group (use inside parallel/pipeline stages) |
| `cwd` | flow cwd | must be the flow cwd or a subdirectory |
| `timeout` | 3600 | wall-clock seconds, hard kill |
| `stall_timeout` | 300 | kill+retry if no output for this many seconds |
| `retries` | 2 | retries for process failure (exit/timeout/unparseable) |
| `schema_retries` | 2 | extra fresh-call retries when output fails schema validation |
| `sandbox` | None | `True` = force OS sandbox, `False` = force off, `None` = harness default (codex/devin default sandboxed; claude/cursor use their own CLI defaults) |
| `web_search` | True | allow the agent to search the web |
| `env` | None | extra env vars (dict of str→str) |
| `extensions` | None | pi only: list of extension paths, each passed as `-e` |
| `trust_project` | True | pi only: `False` passes `--no-approve`, ignoring project-local `.pi/` settings and extensions |

**Returns `None` on terminal failure** (after retries) — never raises for
agent failure. Fan-outs degrade instead of exploding; filter with
`[r for r in results if r]`. The failure reason is journaled and visible in
`cacli flow show <run_id>`.

### `parallel(thunks) -> list`

Runs a list of zero-arg callables concurrently, returns results in input
order. A thunk that raises resolves to `None` (logged). Barrier: waits for all.
Max 4096.

### `pipeline(items, *stages) -> list`

Each item flows through all stages independently — **no barrier between
stages** (item A can be in stage 2 while item B is in stage 1). Stage
signature: `stage(prev_result, original_item, index)`. A stage that raises or
returns None drops that item to `None` and skips its remaining stages. Result
list is index-aligned with items. Prefer this over chained `parallel()` calls
unless a stage genuinely needs ALL previous results (dedup, early exit).

### `phase(title)` / `log(message)`

`phase` groups subsequent agents in progress output; `log` prints a narrator
line and records it in the run's events.

### `flow_args`

The parsed `--args` JSON value, or `None`.

### `budget_total()` / `budget_spent()` / `budget_remaining()`

Dollars. `budget_remaining()` is infinite when no budget is set — guard loops:

```python
while budget_total() is not None and budget_remaining() > 0.50:
    ...
```

When the budget is exceeded, the next `agent()` call raises (in-flight agents
finish and their results are preserved). There is also a lifetime agent cap
(default 500, `META["max_agents"]`) as the runaway backstop.

## Structured output (`schema=`)

Pass a JSON Schema dict with top-level `"type": "object"` or `"array"`. The
returned value is a validated dict/list. Implementation is per harness
(native flags for claude/codex, prompt-enforcement for devin/cursor) but
transparent to the script; all harnesses' outputs are validated host-side and
invalid responses are retried with the validation error fed back. After
`schema_retries` exhaustion the call returns `None` (journaled as
`schema_exhausted` with the last raw candidate).

## Language subset (Monty sandbox)

Works: async/await, lambdas (incl. default-arg capture `lambda x=x:`),
closures, list/dict/set comprehensions, f-strings, `zip`/`enumerate`/builtins,
try/except/finally, starred calls, `import json`, `import re`, `import math`.

Does NOT work: classes, generators/`yield`, `async for`/`async with`,
decorators, any other imports (`functools`, `itertools`, third-party, ...),
`open()`, `os.environ`, `eval`/`exec`. **No forward references**: a function
body may only reference names already defined above it in the file — define
helpers before the functions that use them.

Determinism convention: anything feeding an `agent()` prompt should be
deterministic across runs (no `datetime.now()` in prompts) or `--resume`
cache hits will never match.

## Failure semantics

| event | effect |
|---|---|
| agent fails terminally | that call returns `None`; run continues |
| stage raises in `pipeline` | item → `None`, remaining stages skipped |
| thunk raises in `parallel` | that slot → `None` |
| `main()` raises | run status `failed`, journal intact, resumable |
| budget exceeded | next `agent()` raises `RuntimeError("FlowBudgetExceeded: ...")`; catchable |
| Ctrl-C / kill | `agent()` raises `RuntimeError("FlowCancelled: ...")`; `finally:` blocks run |

## CLI

```bash
cacli flow run flow.py [--args JSON] [--budget 10.00] [--harness claude] [--provider claude-api]
                       [--concurrency N] [--resume RUN_ID] [--cwd DIR]
cacli flow runs               # list past runs: id, status, name, cost
cacli flow show <run_id>      # meta, events, journal, result
```

Runs live in `~/.cacli/flows/<run_id>/` with per-agent raw logs under
`agents/` — when a call returns None unexpectedly, read its log.

## Resume

`--resume <run_id>` replays the longest unchanged prefix of agent calls from
the previous run's journal (instant, no re-execution — though replayed costs
still count against the resumed run's budget), then goes live at the first
call whose identity differs. Identity includes prompt/harness/provider/model/effort/
schema/cwd plus the execution options `web_search`, `settings`, `sandbox`,
`env`, `retries`, `schema_retries`, `timeout`, and `stall_timeout` (and pi's
`extensions`/`trust_project` when set). `label` and
`phase` are display-only and deliberately excluded from resume identity.
Resuming with an edited script or different `--args` is the intended
iteration workflow: fix a later stage, keep earlier results cached. Cached
terminal failures (`None`) replay as `None`. Budget-exceeded runs resume with
a higher `--budget`, completing from the cached prefix.

Structure flows with stable/cheap calls first, volatile ones last — reordering
early calls invalidates everything after them.

## Cost patterns

- Cross-vendor verification kills correlated hallucinations: have one vendor's
  findings refuted by a different vendor.
- Route by task shape: frontend → claude, careful refactors → codex, bulk/cheap
  classification → a small model via `model=`.
- Harnesses that don't report costs (subscription CLIs) contribute per-call
  estimates to the budget, flagged in totals as estimated.
