# Code Review: `cacli` Flow Engine — Confirmed Defects

Thirteen confirmed defects, ordered by severity and impact. The top cluster is sandbox-escape/security issues; the middle is reliability (hangs, leaks, budget/resume correctness); the tail is hardening and resource-exhaustion issues.

---

## Major

### 1. Untrusted `$ref` in workflow schemas triggers host-side network/file fetches (sandbox escape)
**File:** `src/cacli/flow/schema.py:116`

**What breaks:** `validate_output` calls `jsonschema.validate(candidate, schema)` with no custom registry. jsonschema 4.26.0's default retriever (`_warn_for_remote_retrieve`) performs a real `urlopen` on any remote `$ref` before warning — and `urllib` honors `file://` URIs too. Host-side validation of a workflow-supplied schema thus gives sandboxed workflows arbitrary host GET/file-read capability.

**Failure scenario:** A workflow supplies `{"type":"object","$ref":"http://127.0.0.1:PORT/probe"}` (which passes `host.py`'s dict/serializable/≤100KB/top-level-type checks) and induces an agent to return an object. `engine.py:396` runs `validate_output` on the host and performs an HTTP GET to the internal URL. The retry loop can fire it repeatedly.

**Suggested fix:** Pass a `referencing.Registry(retrieve=...)` that refuses remote retrieval (or strip/reject any `$ref` during `host.py` schema validation) so `$ref` never triggers I/O.

---

### 2. CWD confinement has a symlink check-to-use (TOCTOU) race
**File:** `src/cacli/flow/host.py:72`

**What breaks:** `_validate_cwd` does `.resolve()` plus a lexical `relative_to(self.cwd)` check and returns a bare pathname — no dirfd, `O_NOFOLLOW`, or inode pin. The validated string is reused unchanged at spawn time (`aexec.py:78-88` `create_subprocess_shell(cwd=...)`), which re-follows the final path component.

**Failure scenario:** With concurrency 1, agent B validates `cwd="sub"` then parks on the semaphore for the duration of agent A's run. Agent A replaces `sub` with a symlink to an external directory (within its normal write access to the flow workspace). When B starts, the OS follows the replacement and roots B's provider workspace outside the declared flow directory.

**Suggested fix:** Re-resolve and re-check confinement at spawn time, or open an anchored directory handle (`O_NOFOLLOW`/dirfd) at validation and launch relative to it.

---

### 3. Resume keys omit execution-affecting agent options → stale results replayed
**File:** `src/cacli/flow/engine.py:184`

**What breaks:** The per-call resume key is derived only from `{prompt, provider, resolved model, effort, schema, cwd_key, isolation}`. Execution-affecting kwargs — `web_search`, `settings`, `sandbox`, `env`, `retries`, `timeout`, `stall_timeout`, `schema_retries` — are excluded, though they change `build_command`/`exec_fn` behavior and terminal success/failure. On resume, the unchanged key replays the old cached entry (including a cached terminal `None`), so behavior-changing edits are silently ignored.

**Failure scenario:** Run `agent("x", retries=0)` against a flaky provider → terminal `None` cached. Edit to `retries=3` (or flip `sandbox`/`web_search`/`settings`/`env`) and resume → the stale `None` replays instantly and the edit has no effect. Contradicts SKILL.md, which implies only `label` is outside resume identity.

**Suggested fix:** Include all execution-affecting kwargs in the resume-key hash (and/or invalidate the journal when the script/options hash changes).

---

### 4. Parallel-pipeline chained keys are assigned in completion order → non-deterministic resume divergence
**File:** `src/cacli/flow/engine.py:129`

**What breaks:** `JournalKeyChain` hashes each call over the full preceding call sequence, and `_assign_call` assigns keys in the arrival order of concurrent `agent()` coroutines. `pipeline()` issues each item's stage-2 call only after its stage-1 await completes, so stage-2 chain positions depend on live subprocess completion order. A single resume miss permanently sets `replaying=False` and emits `diverged`, rerunning the remaining pipeline at real cost.

**Failure scenario:** In a two-item pipeline, if the live run's stage-1 completions finish in the opposite order from the cached run, both stage-2 keys mismatch and an otherwise-unchanged resume diverges and reruns. The only ordering test covers flat `parallel()` (issued upfront in script order), not completion-order-dependent pipeline issuance.

**Suggested fix:** Derive chain positions from a deterministic structural index (script issue order / pipeline item index) rather than callback-arrival order.

---

### 5. Queued fan-out calls bypass budget enforcement → large overshoot
**File:** `src/cacli/flow/engine.py:260`

**What breaks:** Budget is enforced only in `_assign_call` (`spent >= budget`), which runs at line 184 — *before* the semaphore is acquired at line 260. `parallel()` gathers all thunks concurrently, so every branch passes the check while `spent` is still 0 and then queues. `_account_cost` increments `spent` but never re-checks the budget or sets `cancel_event`, and there's no recheck at semaphore acquire.

**Failure scenario:** `concurrency=1`, `$0.20` budget, `parallel()` of 100 agents: all 100 pass the budget check at 0 spend and queue. After the first `$0.25` result lands, the other 99 still execute serially — overshooting by ~100x.

**Suggested fix:** Re-check the budget after acquiring the semaphore (and/or set `cancel_event` in `_account_cost` when `spent >= budget`) so queued calls abort once the budget is breached.

---

### 6. Failed/retried provider calls are unaccounted for (zero-cost) for estimate-only providers
**File:** `src/cacli/flow/engine.py:424`

**What breaks:** `cost_estimated = succeeded and total_cost is None`, and `effective_cost` is `None` when a call both fails (`final_result is None`) and reports no real cost. For Cursor/Devin (which never populate `total_cost`), an exhausted structured-output call yields `succeeded=False → cost_estimated=False → effective_cost=None`. `_account_cost` returns immediately on `None`, so `spent` and the journal record zero cost despite up to `(retries+1)×(schema_retries+1)` paid subprocess invocations.

**Failure scenario:** A Cursor/Devin `agent(schema=...)` call that produces invalid JSON through all schema retries consumes several paid calls but records `$0`; the budget gate never fires, and repeated failures continue until the `max_agents` cap without reducing `budget_remaining()`.

**Suggested fix:** Charge the estimated cost per completed provider attempt regardless of success/parse outcome (account cost on each attempt, not only on a successful terminal result).

---

### 7. Process-group shutdown stops escalating once the shell leader exits → TERM-resistant descendants defeat hard timeouts and hang `aexec`
**File:** `src/cacli/flow/aexec.py:57`

**What breaks:** `_stop_process` sends SIGTERM to the process group, then waits up to `grace` on `proc.wait()` — which tracks only the shell *leader*. If the leader dies within grace (normal for shells), the function returns before the SIGKILL escalation, so a TERM-ignoring descendant survives. Because the subprocess is spawned with `stdout/stderr=PIPE`, that descendant inherits the pipe write ends; after the timeout break, `await asyncio.gather(*readers)` blocks in `stream.read(4096)` until an EOF that never arrives, so `aexec` hangs indefinitely and the process leaks. No outer `wait_for` in `engine.py` rescues it.

**Failure scenario:** A provider command starts a child that ignores SIGTERM and inherits stdout/stderr. On timeout/stall the shell exits on SIGTERM, `_stop_process` returns without SIGKILLing the group, and `aexec` blocks forever on the inherited pipes.

**Suggested fix:** Always SIGKILL the process group after the grace window (or re-signal after leader exit), and bound the reader `gather` with a timeout.

---

### 8. One-shot cancellation watcher can miss subprocesses registered concurrently with cancellation
**File:** `src/cacli/flow/engine.py:87`

**What breaks:** `_watch_cancel` is one-shot: it awaits `cancel_event`, terminates a snapshot of `self._processes`, and returns without re-arming. Registration happens only after `create_subprocess_shell` completes, and `_register_process` doesn't re-check `cancel_event`. SIGINT only sets the event (no task cancellation), and `aexec`'s loop checks only timeout/stall.

**Failure scenario:** SIGINT arrives while `create_subprocess_shell` is awaiting creation but before `on_process` registers it. `_watch_cancel` sees an empty set and exits; the new subprocess then registers and runs to its normal timeout. `engine.close()` (which would kill stragglers) sits in `run_flow`'s finally and, in the single-agent case, is blocked on the stuck subprocess itself.

**Suggested fix:** Re-check `cancel_event` in `_register_process` (kill immediately if already set) and/or keep the watcher armed to terminate late registrations.

---

### 9. Engine shutdown doesn't cancel/await queued eager agent callbacks → work continues after completion
**File:** `src/cacli/flow/engine.py:74`

**What breaks:** Monty starts `agent()` calls eagerly (a direct alias of `_ctx_agent`), so unawaited agents run as live host coroutines. `close()` only cancels `_watch_cancel` and terminates currently-registered processes; queued `agent()` coroutines parked on the semaphore/`_index_lock` are never tracked, cancelled, or awaited. `run_flow` awaits `execute()` then `close()` and marks the run "completed."

**Failure scenario (reproduced):** A workflow eagerly creates 6 agents without awaiting and returns. `run_flow` returns `status="completed"` at t≈0.02s with only 2 launches; the 4 queued callbacks then acquire the semaphore and launch fresh subprocesses at t≈0.32s/0.62s — after `close()` and after completion — mutating the completed run's `meta.json` (succeeded 0→6, cost $0→$0.06 post-completion).

**Suggested fix:** Track all spawned `agent()` tasks and cancel/await them in `close()` before marking the run complete.

---

## Minor

### 10. Workflow-controlled schema regexes run synchronously with no complexity bound → host DoS (ReDoS)
**File:** `src/cacli/flow/schema.py:116`

**What breaks:** `validate_output` runs `jsonschema.validate` synchronously on the host event loop; jsonschema uses Python's backtracking `re` engine for `pattern`. The agent-controlled `candidate` is matched against an author-controlled `schema` with no regex safety check, timeout, or thread isolation, and the per-agent timeout wraps only the subprocess exec — not validation.

**Failure scenario:** A schema with `pattern: "^(a+)+$"` plus an agent response like many `a`s followed by `!` triggers catastrophic backtracking (verified in-repo: 20+ seconds and climbing on a ~40-char input), freezing the single event-loop thread and all concurrent flows.

**Suggested fix:** Run validation in a worker thread/process with a wall-clock timeout, or reject/limit `pattern` complexity (or use a linear-time regex engine) during schema validation.

---

### 11. Numeric validation accepts NaN → both wall-clock and stall timeouts disabled
**File:** `src/cacli/flow/host.py:31`

**What breaks:** `--args` is parsed with plain `json.loads`, which accepts the literal `NaN`; the value survives into the Monty sandbox as a native float (no JSON round-trip). `_positive_number` rejects only via `value <= 0 or value > maximum`, both of which are `False` for NaN, so NaN passes as a valid timeout. In `aexec`, `now - started >= timeout` and `now - last_output_at >= stall_timeout` are always `False`, so neither hard kill can fire.

**Failure scenario:** CLI args contain `{"n":NaN}` and the workflow passes `timeout=flow_args["n"], stall_timeout=flow_args["n"]` to a nonterminating agent; the exec loop spins until the process exits on its own, unbounded by either timeout (the sandbox's `max_duration_secs` measures interpreter CPU time and doesn't bound the subprocess).

**Suggested fix:** Reject non-finite values in `_positive_number` (`math.isnan`/`math.isinf`) and disallow `NaN`/`Infinity` in `--args` parsing (`parse_constant`).

---

### 12. Agent stdout/stderr captured without a byte limit → host memory exhaustion
**File:** `src/cacli/flow/aexec.py:104`

**What breaks:** `read_stream` loops `stream.read(4096)` and unconditionally appends every chunk to in-memory lists with no cap, truncation, or rolling window. Each chunk also resets `last_output_at`, so a continuously-emitting process never trips the stall watchdog. The only remaining bound is the wall-clock timeout, which the workflow controls (up to 86,400s).

**Failure scenario:** A provider invocation that streams output continuously (e.g. an agent induced to run `yes` or `cat` a huge file) defeats the stall watchdog and accumulates unbounded strings until the day-long wall timeout or host memory exhaustion.

**Suggested fix:** Cap captured output per stream (truncate or keep a bounded rolling buffer) and kill the process when the cap is exceeded.

---

### 13. Retry backoff holds the subprocess semaphore → queued agents starved
**File:** `src/cacli/flow/engine.py:377`

**What breaks:** The entire retry loop runs inside `async with self.semaphore`, and `_backoff(delay)` (a plain cancellable sleep) is awaited at line 377 while the slot is still held. Defaults make this reachable out of the box (`retry_delays=(15.0, 60.0)`, `retries=2`).

**Failure scenario:** With concurrency 2, two agents that fail their first attempt hold both semaphore slots through the 15s/60s backoffs with no subprocess running, blocking queued agents from starting. This is a throughput/starvation inefficiency, not a correctness failure — the workflow still completes.

**Suggested fix:** Release the semaphore around the backoff sleep (re-acquire before the next attempt), or requeue the retry so the slot is free while waiting.

---

## Minor (local-privacy)

### 14. Flow artifacts inherit the process umask instead of being owner-only
**File:** `src/cacli/flow/runs.py:110`

**What breaks:** `FlowRun.create` builds the run tree with default `Path.mkdir` (0o777 → 0755 under umask 022) and writes all artifacts with plain `write_text`/`open` (0644). No `chmod`/`umask`/`os.open`/`mkstemp`/`mode=` appears anywhere in `src/cacli`, so prompts, args, logs, results, and per-agent schemas are world-readable.

**Failure scenario:** On a multi-user machine with a traversable home directory and the common `022` umask, another local account can read the run directory (0755) and all workflow artifacts (0644), exposing potentially sensitive prompts, arguments, and results.

**Suggested fix:** Create `~/.cacli/flows` and each run directory `0700` and write artifacts `0600` (e.g. set restrictive `mode=` / `os.chmod`, or an `os.open` with a tight mode).