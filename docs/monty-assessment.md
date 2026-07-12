# Technical assessment: Monty for cacli workflow scripts

## What Monty is

Monty is a bytecode-based Python interpreter written in Rust, using Ruff’s Python parser but its own compiler, VM, heap, object model, scheduler, resource tracker, and limited standard library. The current Python API runs each session in a dedicated `monty` worker subprocess, so interpreter crashes do not crash cacli; external functions execute in cacli’s host process.

It is actively developed and unusually well-tested for its age—522 Python language fixtures, 25 Python-binding test modules, 31 core Rust integration-test files, protocol/pool tests, fuzzing, coverage CI, and Linux/macOS/Windows CI—but it explicitly describes itself as “experimental” and “not ready for prime time.” Current main is `0.0.19-beta.4`; the newest stable PyPI release is `0.0.18`, while the subprocess-pool API assessed here is in the `0.0.19b*` prereleases. Rapid releases since January 2026 and a major API/architecture transition imply significant API churn risk.

## Supported subset vs. cacli’s needs

| Construct in proposed workflows | Status | Assessment |
|---|---|---|
| `async def main(ctx)` | Supported | Async functions, nested async functions, and top-level `await` work. |
| `await ctx.agent(...)` | Supported with embedding shim | Async external host callables are converted to futures, run on the host event loop, and resume the VM when complete. |
| Multiple concurrent host calls | Supported | `asyncio.gather(*awaitables)` cooperatively schedules sandbox coroutines and concurrent host futures. There is no sandbox event loop; the host drives concurrency. |
| Lambda expressions | Supported | Extensively tested, including nested lambdas, varargs, keyword-only args, and `**kwargs`. |
| Default-argument capture such as `lambda p=p` | Supported | Default evaluation and captured/default closures are explicitly tested. |
| Closures over `ctx`, schemas, loop variables | Supported | Sync and async closures, transitive closures, and external calls from closures are tested. |
| `lambda f, *_:` | Supported in current code/tests | Varargs parameters and starred calls are tested. `limitations/language.md` still claims some starred expressions are rejected, so documentation is stale and cacli should maintain compatibility tests against its pinned version. |
| List comprehensions | Supported | Multiple clauses, filters, destructuring, nested comprehensions, and closure access work. |
| Dict/set comprehensions | Supported | Supported. |
| Generator expressions | Divergent | They eagerly materialize as lists rather than lazy generators. Usually harmless for the example, but memory and side-effect timing differ from CPython. |
| `zip` | Supported | Included in builtins and suitable for `zip(findings, verified)`. |
| f-strings | Supported | Conversion flags, format specs, and debug syntax are implemented. |
| Dict/list/set/tuple literals | Supported | Nested containers and common unpacking work. |
| `try`/`except`/`else`/`finally` | Supported | Host callback exceptions can be caught when mapped to Monty’s built-in exception hierarchy. |
| Simple classes | Supported with limits | No inheritance, metaclasses, decorators, `super`, classmethod/staticmethod/property decorators, or general dunder protocol. A simple sandbox-side `Ctx` shim class is feasible. |
| `ctx.parallel(thunks)` | Not directly host-embeddable | Monty-defined lambdas/closures cannot cross into host Python as callable objects. They degrade to a repr-like string at the value boundary. `parallel` must execute the thunks inside Monty using `asyncio.gather`. |
| `ctx.pipeline(items, callback)` | Not directly host-embeddable | Same callback-boundary issue. Pipeline iteration and callback invocation must live in a sandbox-side shim. |
| `asyncio.create_task`, semaphore, queue, sleep | Unsupported | Only `asyncio.run` and `asyncio.gather` are exposed. This is enough for the proposed wrapper because cacli can enforce concurrency in the host-side `agent` callback. |
| `async for`, `async with`, async comprehensions | Unsupported | Rejected at parse time. |
| Generator functions / `yield` | Unsupported | Would constrain future DSL expansion. |
| Arbitrary imports / third-party packages | Unsupported by design | Only fixed built-ins: `asyncio`, `datetime`, `json`, `math`, `os`, `pathlib`, `re`, `sys`, `typing`. |
| `functools`, `itertools`, `collections`, `inspect`, etc. | Unsupported | Workflow authors will encounter subset limitations beyond the initial examples. |
| `eval`, `exec`, `compile`, `__import__` | Deliberately absent | Good for isolation. |
| Custom exception identity | Not preserved | A host `FlowCancelled` subclass becomes the nearest Monty built-in category, usually `Exception`. `finally` still runs, but sandbox code cannot reliably catch the original custom class. |

The example workflow itself is within Monty’s language subset. The biggest incompatibility is architectural: sandbox functions and closures cannot be handed to host methods, so `ctx.parallel([...lambdas...])` and `ctx.pipeline(items, lambda...)` cannot simply invoke methods on a normal host `FlowContext`.

## Embedding API

### Recommended architecture

Expose only flat, validated host capabilities such as `_ctx_agent`, `_ctx_phase`, and `_ctx_log`. Append a trusted sandbox-side shim that constructs the user-facing `ctx` object and implements callback-consuming operations inside Monty.

A simplified shim:

```python
# Trusted source appended after the user workflow.
import asyncio

class _WorkflowContext:
    def __init__(self, args):
        self.args = args

    def phase(self, title):
        return _ctx_phase(title)

    def log(self, message):
        return _ctx_log(message)

    async def agent(
        self,
        prompt,
        provider='claude',
        model=None,
        effort=None,
        schema=None,
        label=None,
        phase=None,
        timeout=3600,
    ):
        return await _ctx_agent(
            prompt,
            provider=provider,
            model=model,
            effort=effort,
            schema=schema,
            label=label,
            phase=phase,
            timeout=timeout,
        )

    async def parallel(self, thunks):
        async def run_one(thunk):
            try:
                return await thunk()
            except Exception as exc:
                _ctx_log(f'parallel branch failed: {exc}')
                return None

        return await asyncio.gather(*(run_one(thunk) for thunk in thunks))

    async def pipeline(self, items, stage):
        async def run_one(item, index):
            try:
                return await stage(item, item, index)
            except Exception as exc:
                _ctx_log(f'pipeline item failed: {exc}')
                return None

        return await asyncio.gather(
            *(run_one(item, index) for index, item in enumerate(items))
        )

await main(_WorkflowContext(_ctx_args))
```

Host-side execution would look approximately like:

```python
import pydantic_monty

async def run_workflow(source, flow_ctx, args):
    async with pydantic_monty.AsyncMonty(
        request_timeout=10,
    ) as pool:
        async with pool.checkout(
            script_name='workflow.py',
            limits={
                'max_duration_secs': 2.0,
                'max_memory': 32 * 1024 * 1024,
                'max_allocations': 250_000,
                'max_recursion_depth': 200,
            },
            type_check=True,
            type_check_stubs=WORKFLOW_STUBS,
        ) as session:
            return await session.feed_run(
                source + TRUSTED_CTX_SHIM,
                inputs={'_ctx_args': args},
                external_lookup={
                    '_ctx_agent': flow_ctx.agent,
                    '_ctx_phase': flow_ctx.phase,
                    '_ctx_log': flow_ctx.log,
                },
            )
```

A coroutine returned by `flow_ctx.agent` is detected by `AsyncMonty`, converted to a host future, and awaited without blocking cacli’s event loop. Multiple branches under sandbox `asyncio.gather` cause multiple host coroutines to run concurrently. cacli’s normal host-side semaphore, budget, cancellation, timeout, subprocess lifecycle, and schema validation remain authoritative.

### Why not pass the real `ctx` object?

Arbitrary Python objects are rejected by input conversion. Monty does support registered host dataclasses and dispatches public method calls back to Python, including async methods, but this is not a good direct representation of `FlowContext`:

- The dataclass is serialized into sandbox data and reconstructed for method dispatch; it is not an opaque reference preserving arbitrary host state.
- Private fields are omitted.
- A token-only dataclass proxy could delegate through a host registry, but that adds identity/lifetime machinery.
- Most importantly, Monty lambdas and closures cannot become Python callables across the boundary, so `ctx.parallel(thunks)` and `ctx.pipeline(..., stage)` would still fail.

A sandbox shim over flat host functions is simpler, safer, and preserves the intended DSL.

### Boundary values

Common workflow values cross cleanly: `None`, booleans, integers, floats, strings, bytes, lists, tuples, dicts, sets/frozensets, dates, exceptions, and registered dataclasses. cacli must separately validate that final output and journal values are JSON-compatible, because Monty can return values such as bytes, tuples, sets, and non-string dict keys that `result.json` cannot encode.

Protocol bounds include a 256 MiB frame cap, nesting limits during conversion/decoding, and a 1 GiB per-frame decoded host-memory budget.

## Isolation and limits

### Enforced properties

- The worker starts with an empty environment; Windows retains only `SystemRoot`.
- Sandboxed Python has no access to CPython objects, FFI, native extension modules, sockets, subprocesses, threads, multiprocessing, `ctypes`, or dynamic code compilation.
- Imports are restricted to Monty’s fixed built-in modules. There is no `sys.path`, site-packages, package loader, or `__import__`.
- No filesystem is visible by default. `/tmp`, the current directory, home, `/proc`, `/etc`, and devices are unavailable unless cacli explicitly supplies a mount.
- Without an `os=` handler, `os.getenv`/`os.environ` cannot expose host environment values.
- Filesystem mounts, if used, have canonicalization, traversal checks, symlink-boundary checks, virtual POSIX paths, and read-only/read-write/overlay modes. cacli should supply no mounts for workflow scripts.
- The Python API uses worker subprocesses. A Rust panic, stack overflow, allocator abort, or segfault kills the worker, raises `MontyCrashedError`, and causes pool replacement rather than taking down cacli.
- Worker resource controls include approximate memory accounting, allocation count, cumulative bytecode execution duration, recursion depth, GC interval, AST nesting limits, and numerous amplification pre-checks.
- `request_timeout` is a host watchdog that kills a stuck worker.
- Resource-limit errors are uncatchable in sandbox code and poison the session.

### Important qualifications

- `max_memory` tracks estimated user-visible object memory; it is explicitly not a hard RSS ceiling.
- There is no explicit instruction-count budget. Execution time is checked at bytecode boundaries.
- `max_duration_secs` counts only interpreter execution. Time waiting for `ctx.agent` is paused, so every host agent call still needs cacli’s own wall-clock timeout.
- Host callbacks are the real capability boundary. `_ctx_agent` must validate provider, model, cwd, environment, worktree mode, limits, schema size, and every other option; never treat arguments from the sandbox as trusted.
- The worker subprocess is crash isolation, not an OS security sandbox such as seccomp/container isolation. A memory-safety bug that achieves native code execution inside the Rust worker is outside Monty’s threat model and would inherit the worker process’s OS user privileges. Empty environment and lack of normal syscall surfaces reduce exposure, but do not contain a true native sandbox escape.
- Worker binary resolution checks an explicit path, `MONTY_BIN`, environment scripts, `PATH`, and fallbacks. cacli should pin the installed binary path rather than trust a mutable `PATH`.
- Given the project’s alpha/experimental status, I would describe the safety posture as strong defense for “untrusted-ish” workflow authors, not a high-assurance boundary for actively hostile tenants on a shared server.

## Snapshot/resume

Monty’s snapshot mechanism is highly relevant. `feed_start` returns control at each external function call, name lookup, OS call, or future-resolution barrier. A snapshot exposes the function name, args, kwargs, and call ID; `snapshot.dump()` serializes the entire suspended VM, and a fresh worker/session can restore it with `load_snapshot()`.

For an agent call, cacli could:

1. Receive an `AsyncFunctionSnapshot` for `_ctx_agent`.
2. Persist the snapshot bytes and journal the call arguments.
3. Resume it with `{'future': ...}` so other `parallel` branches can reach their agent calls.
4. Launch the provider subprocess in the host.
5. Eventually receive an `AsyncFutureSnapshot` listing pending call IDs.
6. Resume one or more IDs with `{'return_value': result}` or an exception.
7. Persist the newest VM snapshot after each durable transition.

This is a real continuation, not replay: local variables, comprehension state, closures, coroutine stacks, and control flow resume exactly where suspended.

It should complement, not immediately replace, cacli’s journal:

- Host subprocess lifecycle, costs, retries, cancellation, output files, and idempotency still require a durable journal.
- Pending host coroutine/task objects are not stored in a snapshot. A restored `FutureSnapshot` must be manually resolved from journaled results.
- Dumps restore only on the same Monty version, so exact version pinning is mandatory. Journal replay is more portable across upgrades.
- A dump contains the workflow’s live state, including prompts and results, and must be protected like run data.
- Durable parallel orchestration around `FunctionSnapshot` → `future` → `FutureSnapshot` is substantially more complex than `feed_run`.
- Crash consistency between journaling, launching a provider, and saving the new snapshot still needs careful transaction/idempotency design.

Recommended rollout: retain the existing journal-replay design first, using ordinary `feed_run`. Add continuation snapshots later as an optimization or stronger resume mode after the basic engine is stable. Alternatively, use snapshot resume only at agent-call boundaries while keeping the journal as the authoritative record.

## Performance and packaging

- It is published as [`pydantic-monty`](https://pypi.org/project/pydantic-monty/).
- Current source is `0.0.19-beta.4`; the PyPI stable default remains `0.0.18`, whose documented API predates the current subprocess-pool design. cacli would need an exact prerelease pin or should wait for `0.0.19` stable.
- The current package depends exactly on the same-version `pydantic-monty-runtime`, which ships the `monty` worker binary in a separate platform wheel.
- Release CI builds CPython 3.10–3.14 bindings plus runtime wheels for mainstream Linux, macOS, and Windows and several additional Linux architectures/musl targets.
- Existing stable wheels are roughly 8–10 MB per platform. The beta split changes artifact composition, but remains in that general native-binary class.
- A Rust toolchain is not needed when a matching wheel exists. Source builds require the current Rust toolchain (`rust-version = 1.95` on main) and maturin.
- Warm interpreter work is fast and workflow overhead will be negligible beside coding-agent subprocesses. Subprocess protocol and checkout overhead matter more than the README’s sub-microsecond in-process startup claim, but still should not be material for cacli.

## Verdict

**Use Monty with workarounds**, behind an experimental feature flag and an exact-version compatibility layer.

Required workarounds:

1. Implement `ctx` as a trusted sandbox-side class over flat external host functions.
2. Keep `parallel` and `pipeline` inside Monty so user lambdas never cross the host boundary.
3. Enforce all capability and option validation in host callbacks.
4. Run with no mounts and no OS handler; pin the worker binary path.
5. Configure memory/allocation/execution/recursion limits and retain cacli’s own agent timeout, budget, concurrency, and lifetime-call caps.
6. Validate final results as JSON-compatible.
7. Preserve journal replay initially; treat snapshots as a complementary continuation mechanism.
8. Pin `pydantic-monty==0.0.19b4` and runtime exactly, or wait for `0.0.19` stable. Add a conformance suite covering the full example DSL because current docs and tests disagree on starred syntax.
9. Convert cacli cancellation/budget errors into a small documented built-in exception contract or handle them entirely in the trusted shim, since custom host exception types lose identity.

Monty is a substantially better semantic fit than AST-restricted CPython: it has a purpose-built object-capability boundary, no arbitrary import/native surface, bounded execution, crash-isolated workers, async external calls, and serializable continuations. AST-restricted CPython is easy to get subtly wrong and still leaves a large reflective runtime; a generic CPython subprocess/container provides stronger native containment but is heavier and makes controlled async callbacks/resume much harder.

The caution is maturity: the exact Python API needed here has only just landed in a beta architecture, and Monty’s subset will become part of cacli’s user-facing language contract.

Estimated effort:

- Basic proof of concept with `feed_run`, flat callbacks, shimmed `ctx.agent`/`parallel`/`pipeline`: **3–5 engineering days**.
- Production-quality initial integration with type stubs, limit policy, error translation, cancellation, JSON validation, compatibility tests, packaging, and journal replay: **2–3 weeks**.
- Snapshot-driven durable parallel continuation with crash-safe call-ID journaling and upgrade/version handling: **an additional 1–2 weeks**, likely more if it must be the sole resume mechanism.