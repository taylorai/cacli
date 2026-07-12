# cacli Workflows: a cross-provider agent-orchestration DSL

**Status: planned, not started.** Blocked on landing/stabilizing the devin provider work first.

This is a detailed design for adding a Claude Code-style workflow engine to cacli — a small,
deterministic orchestration layer that lets a script fan out agent calls across *any* supported
provider (claude, codex, gemini, cursor, devin), with pipelining, structured output, resume,
and budget enforcement.

Background: Claude Code ships a `Workflow` tool whose scripts are JS executed in a Node `vm`
sandbox, with `agent()` calls dispatched to in-process subagents. We reverse-engineered its
implementation from the 2.1.201 binary; full notes live in
`~/.claude/workflow-dsl-reference.md` (sections "Implementation notes" and the DSL spec).
This plan adapts that design to cacli's architecture and deliberately diverges where Claude
Code's constraints don't apply to us.

---

## 1. Motivation

cacli already abstracts "run a coding agent" across five providers behind one interface
(`providers/base.py`: `build_command` / `parse_output` → `AgentRunResult`). What it lacks is
*composition*: no way to express "spawn 6 finders in parallel, verify each finding with a
different vendor, loop until dry, stay under $10."

The killer feature Claude Code's version structurally cannot have: **`provider=` per agent
call.** That enables:

- **Cross-vendor judge panels** — Codex implements, Claude reviews, Gemini breaks ties.
- **Racing** — same task to two providers, keep the better diff, kill the loser.
- **Routing by task shape** — frontend → claude, careful refactor → codex, cheap bulk
  classification → gemini/haiku.
- **Vendor-diverse adversarial verification** — a finding must survive skeptics from two
  different model families, which kills correlated hallucinations in a way same-model
  panels can't.

## 2. Design principles (and where we diverge from Claude Code)

| Claude Code choice | Why they did it | cacli decision |
|---|---|---|
| JS scripts in a `vm` sandbox (acorn-validated, codegen disabled) | Scripts bypass the product's permission system; must be safe for arbitrary users | **Python-flavored scripts executed in the [Monty](https://github.com/pydantic/monty) sandbox** (`pydantic-monty`: a Rust-implemented Python-subset interpreter in a crash-isolated worker subprocess). Same reasoning as Claude Code's: the orchestration layer's whole job is to run and assemble agents, so the script itself gets *no* ambient authority — no imports beyond Monty's fixed builtins, no filesystem, no network, no subprocesses, no env. All side effects flow through host callbacks we register. The coding agents the flow spawns are NOT double-sandboxed — `_ctx_agent` runs in the cacli host process with normal access; it's only the orchestration Python that's boxed. See §2.5. |
| Ban `Date.now()` / `Math.random()` | Nondeterminism breaks resume-key chaining | **Mostly free via Monty**: no `time`, `random`, or `uuid` modules exist in the sandbox. Remaining hole is `datetime.now()` — document the convention (anything feeding an `agent()` prompt must be deterministic across runs) and add a load-time lint that warns on `datetime.now`/`.today` in the script source. |
| Token budget (`+500k` output tokens) | They meter tokens internally | **Dollar budget.** `AgentRunResult.total_cost` already exists; cross-vendor dollars are the only meaningful common unit. |
| In-process subagents (same query loop as Task tool) | They own the agent loop | **Subprocesses via provider CLIs** (`claude -p`, `codex exec`, …) using the existing `build_command` path. Devin goes through its existing streaming path. |
| StructuredOutput synthetic tool + Ajv | They can inject tools | **Layered:** native flags where a provider has them, generic prompt-append + `jsonschema` validate + retry elsewhere. See §7. |
| Meta as pure-literal AST extraction | Needed to show name/phases in a permission dialog without executing | **Same technique**: a plain `META` dict at module top, extracted host-side with `ast.literal_eval` on the assignment node — `flow list` never executes untrusted code. |

Everything else we copy nearly verbatim because it's good design: pipeline-over-barrier
semantics, null-on-failure (never reject the whole fan-out), order-chained resume keys,
journal.jsonl, phase-grouped progress, concurrency semaphore, stall/throttle retries.

## 2.5 Script isolation: the Monty sandbox

**Threat model / intent.** Workflow scripts are "untrusted-ish": the point isn't defending
against a hostile tenant, it's that a script whose core purpose is *orchestration* should
be structurally incapable of doing random dangerous stuff (reading `~/.ssh`, phoning home,
shelling out). Capability discipline, not paranoia. The agents a flow spawns keep their
normal powers — we sandbox the conductor, not the orchestra.

**How it works** (validated against the monty source, 0.0.19-beta.4):

- The user's script + a **trusted sandbox-side shim** run inside a Monty worker
  subprocess via `pydantic_monty.AsyncMonty` → `session.feed_run(source, inputs=...,
  external_lookup=...)`. Monty is a Rust interpreter (Ruff's parser, own VM/heap/
  scheduler): no CPython objects, no FFI, no sockets/threads/subprocess/`ctypes`, no
  `__import__`/`eval`/`exec`, imports limited to its fixed builtin modules (`asyncio`
  (only `run`/`gather`), `json`, `re`, `math`, `datetime`, `typing`, ...). Empty env, no
  filesystem mounts, no `os=` handler. Worker crashes raise `MontyCrashedError` in the
  host — they can't take cacli down.
- **Flat host callbacks are the only capabilities**: `external_lookup={"_ctx_agent":
  ..., "_ctx_log": ..., "_ctx_phase": ...}`. Async host functions are detected and
  awaited on cacli's event loop — a sandboxed `await ctx.agent(...)` suspends the VM and
  runs the real subprocess-spawning coroutine in the host. Host callbacks must validate
  every argument (provider, model, cwd, schema size, ...) — the callback boundary IS the
  security boundary.
- **`ctx` is built inside the sandbox** by the trusted shim we append to the user's
  source: a `_WorkflowContext` class whose `agent`/`log`/`phase` delegate to the flat
  callbacks, and whose `parallel`/`pipeline` are implemented *inside* Monty on
  `asyncio.gather`. This is forced, not stylistic: Monty lambdas/closures cannot cross
  the boundary as host-callable objects, so user thunks/stage-callbacks must be invoked
  sandbox-side. Fine — concurrency is still real (N suspended VM calls → N concurrent
  host coroutines), and the host semaphore/budget/journal stay authoritative.
- **Resource limits on the interpreter itself**: `max_duration_secs` (bytecode execution
  time only — clock pauses while awaiting host calls, so agent wall-clock timeouts stay
  host-side), `max_memory`, `max_allocations`, `max_recursion_depth`, plus a
  `request_timeout` watchdog that kills a hung worker.
- **Value boundary**: dicts/lists/str/numbers/None cross cleanly; we validate `main()`'s
  return value is JSON-encodable before writing `result.json` (Monty can hand back
  tuples/sets/bytes that JSON can't take).

**Language subset consequences for flow authors** (document loudly): all our example
patterns work — `async def main(ctx)`, lambdas with default-arg capture, closures,
list/dict comprehensions, f-strings, `zip`, try/except, simple classes. Not available:
generators/`yield`, `async for`/`async with`, arbitrary imports (`functools`,
`itertools`, `collections`, third-party anything), inheritance/decorators. Schemas are
plain dict literals, which is all `schema=` ever needed. `META` is extracted host-side
with CPython `ast.literal_eval` on the assignment (no execution needed for `flow list`).

**Custom exceptions don't keep identity across the boundary**, so `FlowCancelled` /
`FlowBudgetExceeded` are raised sandbox-side by the trusted shim from sentinel error
values returned by the host callbacks (shim-owned exception classes), rather than
host-raised types.

**Maturity hedge**: monty self-describes as experimental; the subprocess-pool API we
need is in the `0.0.19b*` prereleases. Pin `pydantic-monty` + `pydantic-monty-runtime`
exactly, pin the worker binary path (don't trust `PATH`), and ship a DSL conformance
test suite that runs our documented patterns against the pinned version so upgrades are
deliberate. Monty's subset becomes part of cacli's user-facing language contract.

**Future option — VM snapshots**: monty can serialize the entire suspended VM at any
external-call boundary (`feed_start` → `snapshot.dump()` → `load_snapshot()`), i.e. a
real continuation with all locals/closures/coroutine stacks intact. That could later
give us durable pause/resume mid-`parallel` without replay. Journal-replay remains the
authoritative resume mechanism (portable across monty versions, auditable); snapshots
are a Phase 5+ complement, not a replacement.

## 3. User-facing surface

### 3.1 CLI

```bash
cacli flow run myflow.py                       # run a workflow file
cacli flow run myflow.py --args '{"pr": 42}'   # JSON args → ctx.args
cacli flow run myflow.py --budget 10.00        # dollar cap
cacli flow run myflow.py --resume <run_id>     # replay journal prefix
cacli flow run review --args '...'             # named flow from ~/.cacli/flows/ or .cacli/flows/
cacli flow list                                # discoverable named flows (META.name/description)
cacli flow runs                                # past runs: id, flow, status, cost, duration
cacli flow show <run_id>                       # phases, per-agent status/cost, result.json
cacli flow kill <run_id>
```

`flow run` executes in the foreground by default (Ctrl-C = graceful cancel: abort in-flight
subprocesses, journal is already durable). `--detach` wraps the whole flow in the existing
tmux spawn machinery (`spawn.py`) so it shows up in `cacli status` like any other session.

### 3.2 Script format

```python
# review_flow.py
META = {
    "name": "cross-review",
    "description": "Cross-vendor adversarial review of the working diff",
    "phases": ["Find", "Verify", "Synthesize"],   # optional, display only
}

FINDINGS = {
    "type": "object",
    "required": ["findings"],
    "properties": {"findings": {"type": "array", "items": {
        "type": "object",
        "required": ["file", "line", "summary"],
        "properties": {
            "file": {"type": "string"},
            "line": {"type": "integer"},
            "summary": {"type": "string"},
        },
    }}},
}

VERDICT = {
    "type": "object",
    "required": ["refuted"],
    "properties": {"refuted": {"type": "boolean"}, "reason": {"type": "string"}},
}


async def main(ctx):
    ctx.phase("Find")
    finder_specs = [
        ("find correctness bugs in the uncommitted diff", "codex", None),
        ("find security issues in the uncommitted diff", "claude", "opus-4.8"),
        ("find performance problems in the uncommitted diff", "gemini", None),
    ]
    rounds = await ctx.parallel([
        lambda p=p, prov=prov, m=m: ctx.agent(p, provider=prov, model=m, schema=FINDINGS)
        for p, prov, m in finder_specs
    ])
    findings = [f for r in rounds if r for f in r["findings"]]

    verified = await ctx.pipeline(
        findings,
        lambda f, *_: ctx.agent(
            f"Adversarially try to REFUTE this finding; default refuted=true if unsure:\n{f}",
            provider="codex", schema=VERDICT, phase="Verify",
            label=f"verify:{f['file']}",
        ),
    )
    confirmed = [
        f for f, v in zip(findings, verified)
        if v is not None and not v["refuted"]
    ]

    ctx.phase("Synthesize")
    report = await ctx.agent(
        "Write a review summary for these confirmed findings:\n"
        + "\n".join(str(f) for f in confirmed),
        provider="claude",
    )
    return {"confirmed": confirmed, "report": report}
```

Contract: the script defines `META: dict` and `async def main(ctx)`; the trusted shim
appended at run time calls `await main(_WorkflowContext(_ctx_args))` (`ctx.args` carries
`--args`). The return value must be JSON-encodable (validated host-side) and is written
to `result.json` and printed. No imports are needed or allowed beyond Monty's builtins —
schemas are plain dict literals, as above. The script runs sandboxed per §2.5: it cannot
touch the filesystem, network, env, or processes; every effect goes through `ctx`.

## 4. Core API (`ctx`), in full

### 4.1 `ctx.agent(prompt, **opts) → str | dict | None`

Spawns one provider CLI subprocess (async), returns its result message.

| opt | type / default | semantics |
|---|---|---|
| `provider` | str, default `"claude"` (flow-level default overridable via `META["default_provider"]` or `--provider`) | any registered cacli provider |
| `model` | str \| None | passed through `resolve_model` (aliases already handled per provider) |
| `effort` | `"low"\|"medium"\|"high"\|"xhigh"\|"max"` \| None | mapped per provider exactly as `run`/`spawn` do today |
| `schema` | dict (JSON Schema) \| None | forces structured output; returns validated `dict` instead of `str`. See §7. |
| `label` | str \| None | display label; defaults to first 60 chars of prompt, whitespace-collapsed (same rule as Claude Code) |
| `phase` | str \| None | explicit phase-group assignment; overrides the ambient `ctx.phase()` value (required for correctness inside `pipeline`/`parallel` stages — ambient phase is a race there, same caveat as Claude Code) |
| `cwd` | str, default flow cwd | working directory for the subprocess |
| `isolation` | `"worktree"` \| None | run in a fresh `git worktree` (§9) |
| `timeout` | int seconds, default 3600 | hard wall-clock kill |
| `stall_timeout` | int seconds, default 300 | no-output watchdog (§10) |
| `retries` | int, default 2 | retry count for stall/nonzero-exit/schema-exhaustion |
| `web_search` | bool, default True | forwarded to `build_command` |
| `settings` | str \| None | forwarded (claude `--settings`) |
| `sandbox` | bool \| None | forwarded to providers that support it (codex workspace-write, devin bubblewrap) |
| `env` | dict \| None | extra env vars for the subprocess |

Return semantics (copied from Claude Code because they're right):

- success, no schema → `result_message: str`
- success, schema → validated `dict`
- terminal failure after retries (nonzero exit, timeout, unparseable output, schema retry
  cap) → **`None`**, never an exception. The failure is journaled, logged via
  `ctx.log(...)`, and recorded on the run so `flow show` explains every `None`.
- flow cancelled → raises `FlowCancelled` (so `finally:` blocks in scripts run).
- budget exceeded → raises `FlowBudgetExceeded` on the *next* `agent()` call; in-flight
  agents finish and their results are preserved (matches Claude Code semantics).

Rationale for null-not-raise: fan-outs must degrade, not explode. `parallel()` over 20
agents where one hits a provider outage should yield 19 results. Scripts learn one idiom:
`.filter(None)` / `if r is not None`.

### 4.2 `ctx.parallel(thunks) → list`

`thunks: list[Callable[[], Awaitable]]`. Runs all concurrently (bounded by the global
semaphore), **barrier** — returns when all settle. A thunk that raises resolves to `None`
in the result list (logged); the call itself never raises except `FlowCancelled` /
`FlowBudgetExceeded`. Preserves input order. Max 4096 items (explicit error).

### 4.3 `ctx.pipeline(items, *stages) → list`

Each item flows through all stages independently — **no barrier between stages**; item A
can be in stage 3 while item B is in stage 1. Stage callback signature:
`stage(prev_result, original_item, index)` — extra params optional (we inspect arity, or
just document `*_`). A stage that raises (or returns after its agent fails) drops that item
to `None` and skips its remaining stages. Result list is index-aligned with `items`.
This is the default multi-stage primitive; docs will repeat Claude Code's guidance that a
barrier is only justified by genuine cross-item dependency (dedup, early-exit, "the other
findings").

Implementation: one `asyncio.Task` per item running `functools.reduce`-style through the
stages; the agent-level semaphore provides all necessary throttling — the pipeline itself
imposes none.

### 4.4 `ctx.phase(title)` / `ctx.log(message)`

`phase()` sets the ambient phase for subsequently *created* agents (stored on the ctx,
snapshotted at `agent()` call time). `log()` prints a timestamped narrator line to the flow
log and stdout, and appends to the run's `events.jsonl` for the dashboard.

### 4.5 `ctx.args`

Parsed `--args` JSON, verbatim. `None` if absent.

### 4.6 `ctx.budget`

```python
ctx.budget.total       # float | None (dollars)
ctx.budget.spent()     # float — sum of AgentRunResult.total_cost over completed agents
ctx.budget.remaining() # max(0, total - spent()), or math.inf if no total
```

Hard ceiling: once `spent() >= total`, the next `agent()` raises `FlowBudgetExceeded`.
Providers that return `total_cost=None` (subscription CLIs, devin ACUs) contribute a
**per-provider fallback estimate** from a static table in `flow/costs.py`
(e.g. `{"codex": 0.15, "gemini": 0.05, ...}` per call — crude, tunable, better than 0),
and the run summary flags "N calls estimated". Loop guard doc rule carried over verbatim:
`while ctx.budget.total and ctx.budget.remaining() > 0.50:` — `remaining()` is infinite
with no budget set, and there's a lifetime agent cap (default 500, `META["max_agents"]`)
as the runaway backstop, with a `FlowAgentCapError` whose message explains the
budget-loop footgun (Claude Code's exact failure mode).

### 4.7 `ctx.flow(name_or_path, args=None)` — subflows (Phase 5, optional)

Run another flow inline sharing semaphore/budget/cancellation, one nesting level max.
Deferred; `main()` calling ordinary Python functions covers most of this for free (a real
advantage of not sandboxing).

## 5. Engine internals

New package `src/cacli/flow/`:

```
flow/
  __init__.py      # public: run_flow(), FlowHost
  sandbox.py       # Monty session mgmt: AsyncMonty pool, feed_run, limits, META extraction
  shim.py          # trusted sandbox-side source (as a string): _WorkflowContext,
                   # parallel/pipeline on asyncio.gather, shim-owned FlowCancelled etc.
  host.py          # FlowHost: the host callbacks (_ctx_agent/_ctx_log/_ctx_phase),
                   # argument validation, budget, phase state
  engine.py        # agent-call lifecycle: queue → exec → parse → retry → journal
  aexec.py         # async subprocess execution (create_subprocess_shell)
  journal.py       # key chaining, journal.jsonl read/append, resume cache
  schema.py        # structured-output enforcement + jsonschema validation + retry loop
  costs.py         # fallback per-call cost estimates by provider
  worktree.py      # git worktree create/cleanup
  runs.py          # run dirs, run metadata, list/show
  cli.py           # `cacli flow` subcommand wiring
```

Split of responsibilities: the user-visible `ctx` object lives in `shim.py` and executes
inside Monty; `host.py`'s `FlowHost` is its host-side counterpart, reached only via the
flat `external_lookup` callbacks. Everything below the callback line (§4's semantics,
§5.4's lifecycle, journal, budget, worktrees, retries) is unchanged host-side Python.

### 5.1 Async execution path

`runner.run_agent` is sync-by-injection (`exec_fn`). The flow engine adds a native async
equivalent rather than threadpooling the sync one:

```python
# aexec.py
async def aexec(cmd, cwd, timeout, env, on_output=None) -> ExecResult:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=cwd, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    # read stdout incrementally: feeds (a) per-agent log file,
    # (b) stall watchdog, (c) live progress preview
```

Non-breaking: `run_agent`/`exec_fn` stay as-is for `run`/`spawn`. The engine reuses
`build_command` and `parse_output` from the providers unchanged. Env hygiene copied from
`spawn.py`: `unset CLAUDECODE` (and any equivalent nesting guards other providers grow) so
spawned CLIs don't think they're nested.

Incremental stdout matters: `claude -p --output-format=stream-json` and codex's JSONL both
emit per-event lines, which gives us (a) a real stall signal ("no new line in N sec" beats
"process still alive"), (b) `lastToolName`-style progress previews for the dashboard, and
(c) a live token/cost estimate before completion. `parse_output` still runs on the full
captured stdout at the end — no provider changes needed.

### 5.2 Concurrency

One `asyncio.Semaphore(min(16, max(2, os.cpu_count() - 2)))` per run (override:
`META["concurrency"]` or `--concurrency`). Note our agents are *external processes* doing
their own parallel work, so CPU-count heuristics matter less than API rate limits; the cap
is mostly about not melting the machine with 40 concurrent node/CLI startups and not
tripping vendor rate limits. Per-provider sub-limits (`{"devin": 2}`) as a config knob in
`~/.cacli/config.json` — devin sessions are heavyweight/expensive; claude/codex tolerate
10+ fine.

### 5.3 Cancellation

`FlowContext` holds one `asyncio.Event` (cancel flag) + a registry of live `Process`
handles. Ctrl-C / `flow kill`: set flag → `proc.terminate()` all live subprocesses → 10s
grace → `proc.kill()`. Journal already has every completed result, so a killed run resumes
cleanly. `agent()` checks the flag before spawn and raises `FlowCancelled`.

### 5.4 The agent-call lifecycle (engine.py), end to end

1. Check cancel flag, agent cap, budget → maybe raise.
2. Compute journal key (§6). If resume-mode and key present in journal → return cached
   result instantly, emit `cached` progress event, **skip all execution**. First key miss
   flips the run to live mode permanently (divergence point — same rule as Claude Code).
3. Assign index, label, phase; emit `queued` event; await semaphore (+ per-provider limit).
4. If `isolation="worktree"` → create worktree, swap `cwd` (§9).
5. Build command via provider (`resolve_model`, effort mapping, schema handling per §7).
6. `aexec` with wall-clock timeout + stall watchdog on output growth.
7. Parse: `parse_output` → `AgentRunResult`; schema validate if applicable.
8. On failure classes (nonzero exit / stall / unparseable / schema-invalid): retry per
   policy (§10), fresh subprocess each time (provider CLIs are stateless from our side;
   schema retries append the validation error to the prompt).
9. Journal `{type: "result", key, result, cost, provider, model, duration_ms}`; add cost
   to budget; emit `done`/`error` event; cleanup worktree; return result or `None`.

## 6. Journal & resume

Run directory: `~/.cacli/flows/<run_id>/` (`run_id` = `fl_` + 8 hex, mirroring session ids):

```
script.py        # verbatim copy of the flow file as executed
args.json
meta.json        # flow name, start/end, status, budget, totals, cwd, git HEAD at start
journal.jsonl    # {type: "started"|"result", key, index, label, provider, model,
                 #  result?, cost?, duration_ms?, error?}
events.jsonl     # progress stream for dashboard/status (append-only)
agents/<index>-<label-slug>.log   # full raw stdout per agent
result.json      # main()'s return value
```

**Key derivation — order-chained, copied from Claude Code's "v2" scheme** (this is the
load-bearing design decision):

```python
key_n = sha256(key_{n-1} + canonical_json({
    "prompt": prompt,
    "provider": provider, "model": model, "effort": effort,
    "schema": schema, "cwd": cwd, "isolation": isolation,
})).hexdigest()
```

Chaining the previous key means resume replays exactly the **longest unchanged prefix** of
agent calls: edit call #7 and calls 1–6 hit cache while 7+ run live. Corollary we document
loudly: reordering early calls invalidates everything after them, so structure flows with
stable/cheap calls first, volatile ones last. `label` and `phase` are display-only and
deliberately excluded from the key.

Resume flow: `cacli flow run <script> --resume fl_abc123` loads the old journal into a
`{key: result}` dict, runs the script normally; `agent()` consults the dict until first
miss. `--resume` with a *different* script file is allowed and is exactly how you iterate
(fix stage 3, keep stages 1–2 cached). `started`-without-`result` entries indicate an agent
that died mid-flight; log that we're respawning it (Claude Code emits telemetry here; we
just log).

Concurrency caveat (learned from Claude Code's design): with parallel agents, "order" is
*call order in the script*. Keys are computed host-side as `_ctx_agent` callbacks arrive,
so determinism now rests on Monty's scheduler delivering those calls in a stable order.
Monty's VM is single-threaded and cooperative, and the shim's `parallel`/`pipeline` build
their `asyncio.gather` arguments in list order, so each thunk reaches its first
`_ctx_agent` call in list order before any host result can resolve — but this is an
assumption about monty internals, not our own lock anymore. It gets adversarial unit
tests (fan-outs with staggered host-callback completion times must produce identical key
chains across runs), and the conformance suite re-checks it on every monty version bump.

## 7. Structured output (`schema=`)

Three tiers, chosen per provider in `schema.py`:

1. **Native schema flag** — codex: `codex exec --output-schema <tmpfile>`; claude:
   `--output-format json` + (when available) `--json-schema`. Provider gains an optional
   `build_command(..., output_schema=...)` kwarg (default ignore → tier 3).
2. **Native JSON mode, no schema** — provider guarantees JSON shape but not our schema:
   parse then validate ourselves, retry on mismatch.
3. **Prompt-level enforcement (universal fallback)** — append to the prompt:

   ```
   Respond with ONLY a JSON object matching this JSON Schema (no prose, no code fences):
   <schema>
   ```

   Then: extract candidate JSON from the result (strip fences, find outermost braces —
   one robust `extract_json()` with tests, gemini/cursor love fences), validate with
   `jsonschema` (new dep, pure Python). On failure retry a **fresh call** with the prompt
   plus `Your previous response failed validation: <errors>. Previous response: <trunc>.
   Return only corrected JSON.` Cap: 3 schema retries (configurable), then return `None`
   and journal `schema_exhausted` with the last candidate (mirrors Claude Code's
   `lastStructuredOutputInput` — invaluable for debugging).

Fresh-call-with-error-appended loses the failed attempt's context vs Claude Code's
in-conversation nudge, but it's provider-universal and schema failures are usually format
slips, not reasoning failures. Good enough; revisit with per-provider session continuation
(`claude --resume`, `codex exec resume`) only if retry quality is bad in practice.

## 8. Budget details

- `spent()` accumulates as results land (thread-safe via the event loop; no locks needed).
- In-flight agents don't count until they finish → mild overshoot, same as Claude Code;
  acceptable and documented.
- `meta.json` totals: real cost, estimated cost, per-provider breakdown; `flow runs` shows
  `$3.42 (+$0.60 est)`.
- No budget → `remaining() == math.inf`; agent cap is the only backstop.

## 9. Worktree isolation

For parallel *coding* agents (our main event, unlike Claude Code where review fan-outs
dominate), writes must not collide:

- `git worktree add <run_dir>/worktrees/<index> HEAD` (detached), agent `cwd` set there.
- After the agent: `git -C <wt> status --porcelain` → if clean, `git worktree remove`;
  if dirty, **keep it**, record the path + `git diff` stats in the journal entry, and
  return `{"result": <text>, "worktree": <path>, "dirty": true}`-shaped metadata alongside
  the text (exact shape TBD: likely `ctx.agent(..., isolation="worktree")` returns a
  `WorktreeResult` dataclass with `.text`/`.structured`/`.worktree_path`).
- Synthesis stages can then `git -C <wt> diff`, cherry-pick, or hand the path to a merger
  agent. `flow show` lists surviving worktrees; `cacli flow clean <run_id>` prunes.
- Non-git cwd + `isolation="worktree"` → hard error at call time.
- Cost note from Claude Code docs applies (~hundreds of ms + disk per agent): only for
  mutating stages.

## 10. Reliability: timeouts, stalls, retries, throttles

- **Wall-clock timeout** per agent (default 3600s, opt) → kill, counts as retryable failure.
- **Stall watchdog**: no stdout growth for `stall_timeout` (default 300s; provider CLIs
  stream events continuously, so silence is a strong death signal) → kill + retry. Devin's
  streaming wrapper may have legitimately long silent gaps — per-provider default override
  table (`devin: 900`).
- **Retries**: default 2 (so ≤3 attempts), exponential-ish backoff 15s/60s, fresh
  subprocess each attempt, each attempt journaled as `started` (only final success gets
  `result`).
- **Rate-limit/throttle detection**: `parse_output` failures where raw output matches
  known 429/quota/overloaded patterns per provider → longer backoff (120s) and don't
  consume a retry (up to 3 throttle waits). Pattern table lives with each provider.
- Every retry/backoff emits a `ctx.log`-visible event — silent stalls are how orchestrators
  lose user trust.

## 11. Observability

- **Foreground default**: rich-ish live tree (phases → agents with spinner/state/cost),
  falling back to plain line-per-event when not a TTY. (Phase 1 can be plain lines only.)
- **`cacli status` / web dashboard integration**: each flow run registers a synthetic
  `SessionInfo` (`name="flow:cross-review"`, `provider="flow"`) so existing tooling shows
  it; `flow show <run_id>` is the detailed per-agent view reading `events.jsonl`. Dashboard
  gets a `/flows` view later (Phase 4) reading the same files — no server-side state.
- Per-agent raw logs under `agents/` mean any weird result is one `less` away — this is
  the equivalent of Claude Code's `agent-<id>.jsonl` transcripts and non-negotiable.

## 12. Failure semantics summary (the contract scripts rely on)

| event | effect |
|---|---|
| agent fails terminally | that call returns `None`; run continues |
| stage callback raises in `pipeline` | item → `None`, remaining stages skipped for it |
| thunk raises in `parallel` | that slot → `None` |
| `main()` raises | run status `failed`, traceback to log + `meta.json`, journal intact (resumable) |
| budget exceeded | next `agent()` raises `FlowBudgetExceeded`; in-flight complete; if uncaught, run ends with partial `result.json` absent but journal intact |
| Ctrl-C / kill | `FlowCancelled` raised at every pending `agent()`; subprocesses terminated; resumable |

## 13. Testing plan

- **`fake` provider** registered only in tests: `build_command` echoes a JSON blob via
  `printf`, `parse_output` reads it. Enables full-engine tests with zero API calls and
  controllable failures (nonzero exit, garbage output, slow output for stall tests,
  fenced-JSON for extractor tests).
- Unit: journal key chaining (order determinism under `parallel`; the
  synchronous-key-assignment lock gets adversarial tests), resume prefix/divergence,
  pipeline no-barrier semantics (timestamps prove overlap), null propagation, budget
  ceiling + estimate fallback, schema retry loop + `extract_json` corpus (fences, prose
  prefix, trailing text, nested braces), worktree create/keep-if-dirty/cleanup.
- Integration (manual/opt-in, `CACLI_FLOW_LIVE_TESTS=1`): 3-agent flow on
  claude-haiku/gemini-flash with a trivial schema; one codex `--output-schema` round-trip.
- Determinism lint test: script using `datetime.now()` in a prompt triggers the warning.
- **DSL conformance suite** (new, load-bearing given monty's beta status): every
  documented workflow pattern (fan-out, pipeline, loop-until-dry, default-arg lambda
  capture, closure over schema dicts, try/except around `agent()`, shim exceptions) as a
  runnable script against the pinned monty version with the `fake` provider. Runs in CI;
  gates any `pydantic-monty` version bump.
- Shim unit tests: `parallel`/`pipeline` null-on-failure inside the sandbox, exception
  translation (host sentinel → shim `FlowCancelled`/`FlowBudgetExceeded`), JSON-
  encodability rejection of tuple/set/bytes return values.
- Sandbox escape smoke tests: scripts attempting `open()`, `__import__`, `os.environ`,
  oversized allocation, infinite loop (must die by limit, not hang the host).

## 14. Phasing

**Phase 0 — prereqs (small, can land independently)**
- `aexec.py` async subprocess execution with incremental stdout capture.
- Providers: optional `output_schema` kwarg on `build_command` (default: ignore).
- **Monty spike**: pin `pydantic-monty`/`pydantic-monty-runtime`, prove the
  `feed_run` + async `external_lookup` round-trip (sandboxed `await ctx.agent()` →
  host coroutine → result back into the VM) with the example script from §3.2.
- Land after devin work is stable; nothing here touches devin.

**Phase 1 — core engine (the useful MVP, ~700–900 lines)**
- `sandbox.py` + `shim.py` + `host.py`: Monty session, trusted `_WorkflowContext` shim
  (`agent`/`parallel`/`pipeline`/`phase`/`log`/`args`), flat validated host callbacks,
  interpreter limits, `ast.literal_eval` META extraction.
- Semaphore, cancellation, run dirs, `events.jsonl`, per-agent logs, plain-line progress.
- `cacli flow run` (foreground), `flow runs`, `flow show`.
- Null-on-failure semantics, wall-clock timeout, basic retries.
- DSL conformance suite skeleton with the `fake` provider.

**Phase 2 — resume + budget (~200 lines)**
- Journal key chaining, `--resume`, divergence rule, deterministic key assignment.
- Dollar budget + cost estimates table + agent cap.

**Phase 3 — structured output (~250 lines)**
- `schema.py` three tiers, `extract_json`, retry loop, codex/claude native flags.

**Phase 4 — isolation + observability (~300 lines)**
- Worktrees, keep-if-dirty, `flow clean`.
- Live TTY tree, dashboard `/flows` view, `SessionInfo` registration, `--detach`.

**Phase 5 — niceties (as wanted)**
- `ctx.flow()` subflows; helper library `cacli.flow.patterns` (majority_vote, race,
  judge_panel, until_dry — thin, documented, cross-vendor by default); named flows dirs
  (`~/.cacli/flows/`, `.cacli/flows/`); stall/throttle per-provider tuning tables;
  determinism lint; per-provider concurrency limits config.

## 15. Open questions (decide at build time)

1. **Devin in flows**: cost per session vs per-call estimate; does its streaming wrapper
   coexist with `aexec` capture (probably yes — it already emits stream-json); sensible
   `stall_timeout`. Depends on the devin work landing — hence this whole plan waiting.
2. **`WorktreeResult` shape** — dataclass vs plain dict; affects schema+worktree combo.
3. **Session-continuation retries** (claude `--resume`, codex `exec resume`) for schema
   failures — only if fresh-call retry quality disappoints.
4. **Windows** — `create_subprocess_shell` + tmux-free path; punt (cacli already assumes
   tmux for spawn).
5. Whether `cacli run --provider flow` style unification is worth it vs keeping `flow` a
   separate subcommand (leaning: separate; a flow is not an agent).
6. **jsonschema dep** — fine to add? (pure-python, boring). Alternative: vendored minimal
   validator; not worth it. (Validation is host-side in `schema.py`; the sandbox never
   needs it — schemas are just dicts passed through `_ctx_agent`.)
7. **Monty version strategy** — pin `0.0.19b*` now or wait for `0.0.19` stable? The
   subprocess-pool API we depend on only exists in the betas, and dumps/wheels are
   version-locked. Leaning: pin the beta behind the conformance suite and treat bumps as
   deliberate migrations. Also decide the interpreter limit defaults (`max_memory`,
   `max_duration_secs`, ...) and whether they're `META`-overridable.
8. **Snapshot-based resume** (VM continuations, §2.5) — revisit after journal-replay
   resume is stable; candidate for durable mid-`parallel` pause/resume in Phase 5+.

## 16. Reference

- Claude Code Workflow DSL spec + reverse-engineered implementation notes:
  `~/.claude/workflow-dsl-reference.md`. Key ideas adopted: pipeline-default/barrier-rare,
  null-on-failure, order-chained resume keys, journal.jsonl, budget-guarded loops + agent
  cap with explanatory error, stall watchdog + throttle backoff, worktree-per-mutating-agent,
  structured output enforced at a validation layer with bounded retries, "your final text
  IS the return value" subagent prompt convention (we bake an equivalent line into every
  flow agent prompt: results are parsed by a script, not read by a human — worth stating
  explicitly for gemini/cursor especially).


## Addendum 2 (2026-07-11): Phase-0 spike findings — these override earlier sections where they conflict

The Monty spike ran successfully against **`pydantic-monty==0.0.18` (stable)** — the
0.0.19 betas are quarantined by the sfw package firewall, and 0.0.18's in-process API
(`Monty(code, inputs=[...]).run_async(inputs=..., external_functions=..., limits=...)`)
supports everything Phase 1 needs: async external functions awaited on the host event
loop, real fan-out concurrency, `ResourceLimits`, and snapshot dumps. Pin 0.0.18; wrap
all monty API usage inside `sandbox.py` so the later migration to 0.0.19's
crash-isolated worker pool (`AsyncMonty`) touches one module. Note 0.0.18 is in-process:
a VM crash could take cacli down — accepted for now, one more reason to migrate later.

Spike-verified facts that change the design:

1. **0.0.18 does not support `class` definitions at all** (parser rejects them). The
   `_WorkflowContext` shim class is out; the DSL becomes **flat module-level functions**
   injected by the shim: `agent()`, `parallel()`, `pipeline()`, `phase()`, `log()`, and
   a `flow_args` variable. User entry point is `async def main()` (no ctx argument);
   references to `ctx.agent(...)` in earlier sections read as bare `agent(...)`.
2. **No forward references at module level** — a function body can only reference names
   already defined when it's compiled. So the trusted shim is split: a **prelude**
   (imports + the flat functions, prepended *before* the user script) and an
   **epilogue** (`_result = asyncio.run(main())`, appended after). Prepending shifts
   user line numbers: subtract the prelude length when reporting tracebacks.
3. Verified working on 0.0.18: nested async defs, `asyncio.gather`, lambdas with
   default-arg capture, closures, comprehensions, f-strings, `zip`, starred calls,
   try/except around host calls (host exceptions surface as catchable sandbox
   exceptions with message preserved), `json` module. Isolation verified: `open()`,
   `import socket`, `__import__`, `os.environ` all fail; infinite loop killed at
   `max_duration_secs`. Host-callback arrival order is deterministic across runs.
4. The spike lives at `/tmp/monty_spike.py` — seed material for `shim.py` and the
   conformance suite.

**Phase 2 landed (2026-07-11).** Resume uses order-chained keys over original prompts and
validated relative cwd values, with full-prefix replay, permanent divergence, and cached
terminal failures. Dollar budgets include replayed and estimated cost and end uncaught
runs as `budget_exceeded`. The flat budget API is backed by three read-only callbacks;
`budget_remaining()` returns a real positive infinity from the host.

**Post-Phase-2 revisions (2026-07-11).**

- **`agent` is an eager external-function alias, not a sandbox wrapper.** Monty starts
  external calls at call time (JS-promise style), so `t1 = agent(...); t2 = agent(...);
  await t1; await t2` runs both concurrently — spike-verified, conformance-tested. A
  sandbox `async def` wrapper is lazy (CPython-coroutine style) and silently serializes
  that pattern, which is why the Phase 1/2 wrapper was removed. DSL contract
  consequences: all `agent()` args after `prompt` are keyword-only (host signature), and
  calling `agent()` starts the agent even if the result is never awaited. `parallel()`
  remains the blessed fan-out (null-on-failure); deferred-await is fine for small static
  fan-outs.
- The conditional budget-alias injection was removed: the full prelude (budget functions
  included) is always injected — one DSL surface, no content-sniffing. The 0.0.18
  duration-limit perturbation did not reproduce at this prelude size; the suite guards
  it. `max_duration_secs` raised 5s→60s: it only meters interpreter bytecode (pauses
  during host awaits), so its sole job is killing runaway loops; host-side timeouts
  bound everything real.
- Stale-status fix: meta.json records the runner `pid`; `flow runs` reports a `running`
  run whose pid is dead as `interrupted` (journal intact, resumable). Runs created
  before the pid field keep showing `running`.

## Addendum

- Design principles table — what we copy from Claude Code (pipeline semantics, null-on-failure, order-chained resume keys, journal.jsonl, stall watchdogs) and where we deliberately diverge: Python-flavored scripts in the Monty sandbox instead of sandboxed JS (§2.5), dollar budgets instead of token budgets (since AgentRunResult.total_cost already exists), and subprocess-per-agent via your existing build_command/parse_output abstraction.
- Full ctx API spec — agent() with every opt (provider, model, effort, schema, isolation="worktree", timeouts, retries), parallel/pipeline with exact barrier/no-barrier semantics, budget, phase/log, plus the complete failure-semantics contract scripts can rely on.
- The subtle bits called out explicitly: resume keys must be assigned synchronously in script-call order (before the first await) or parallel fan-outs break resume determinism — that one gets its own tests; and worktree results need a keep-if-dirty policy since parallel coding agents are your main case, not review fan-outs.
- Structured output as three tiers: native schema flags (codex --output-schema), native JSON mode + validate, and universal prompt-append + jsonschema + fresh-call retry with the validation error fed back.
- Phased build plan with line estimates: Phase 1 MVP ~500–700 lines (engine + cacli flow run/runs/show), then resume+budget, schema, worktrees+dashboard. Phase 0 is a tiny independent prereq (async exec path) that doesn't touch devin at all.
- Open questions parked for build time — the biggest being devin's cost accounting and stall behavior in flows, which is exactly why this waits for the devin work to land.

One thing I noticed while grounding the plan: cli.py passes sandbox=args.sandbox to build_command/run_agent, but the runner.py and base.py I read don't have that parameter — presumably that's part of the in-flight devin/sandbox work. The plan assumes the post-devin signatures, so worth a quick consistency pass on §4.1's forwarded opts once that lands.
