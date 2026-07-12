"""Workflow agent-call lifecycle."""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

from cacli.flow.aexec import AsyncExecResult, aexec, terminate_process
from cacli.flow.costs import estimated_cost
from cacli.flow.journal import JournalKeyChain, ResumeCache
from cacli.flow.runs import FlowRun
from cacli.providers import get_provider


SleepFn = Callable[[float], Awaitable[None]]
ExecFn = Callable[..., Awaitable[AsyncExecResult]]
ProgressFn = Callable[[dict[str, Any]], None]

FLOW_PROMPT_SUFFIX = (
    "\n\n[Workflow note] Your final response is consumed by a workflow script, "
    "not read directly by a human. Put the complete requested result in the final response."
)


class FlowEngine:
    """Owns concurrency, cancellation, retries, and durable agent records."""

    def __init__(
        self,
        run: FlowRun,
        *,
        concurrency: int,
        max_agents: int = 500,
        cancel_event: asyncio.Event | None = None,
        progress: ProgressFn | None = None,
        exec_fn: ExecFn = aexec,
        sleep: SleepFn = asyncio.sleep,
        retry_delays: tuple[float, ...] = (15.0, 60.0),
        budget: float | None = None,
        resume_cache: ResumeCache | None = None,
    ):
        self.run = run
        self.semaphore = asyncio.Semaphore(concurrency)
        self.max_agents = max_agents
        self.cancel_event = cancel_event or asyncio.Event()
        self.progress = progress
        self.exec_fn = exec_fn
        self.sleep = sleep
        self.retry_delays = retry_delays
        self.budget = budget
        self.spent = 0.0
        self.resume_cache = resume_cache
        self._next_index = 0
        self._keys = JournalKeyChain()
        self._index_lock = asyncio.Lock()
        self._processes: set[asyncio.subprocess.Process] = set()
        self._cancel_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._cancel_task is None:
            self._cancel_task = asyncio.create_task(self._watch_cancel())

    async def close(self) -> None:
        if self._cancel_task is not None:
            self._cancel_task.cancel()
            await asyncio.gather(self._cancel_task, return_exceptions=True)
            self._cancel_task = None
        if self._processes:
            await asyncio.gather(
                *(terminate_process(proc, grace=1.0) for proc in list(self._processes)),
                return_exceptions=True,
            )

    async def _watch_cancel(self) -> None:
        await self.cancel_event.wait()
        if self._processes:
            await asyncio.gather(
                *(terminate_process(proc) for proc in list(self._processes)),
                return_exceptions=True,
            )

    async def _backoff(self, delay: float) -> None:
        sleep_task = asyncio.create_task(self.sleep(delay))
        cancel_task = asyncio.create_task(self.cancel_event.wait())
        done, pending = await asyncio.wait(
            {sleep_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        if cancel_task in done and cancel_task.result():
            raise RuntimeError("FlowCancelled: workflow cancelled")
        await sleep_task

    def _event(self, kind: str, **fields: Any) -> None:
        event = {"type": kind, **fields}
        self.run.event(event)
        if self.progress is not None:
            self.progress(event)

    async def _assign_call(
        self, key_fields: dict[str, Any]
    ) -> tuple[int, str, dict[str, Any] | None, bool]:
        async with self._index_lock:
            if self.budget is not None and self.spent >= self.budget:
                raise RuntimeError(
                    f"FlowBudgetExceeded: spent ${self.spent:.2f} of "
                    f"${self.budget:.2f} budget"
                )
            if self._next_index >= self.max_agents:
                raise RuntimeError(
                    "FlowAgentCapError: agent lifetime cap reached "
                    f"({self.max_agents}); guard unbounded loops with a budget or limit "
                    "(budget_remaining() is infinite when no budget is set)"
                )
            index = self._next_index
            self._next_index += 1
            key = self._keys.next(key_fields)
            cached = None
            respawning = False
            if self.resume_cache is not None and self.resume_cache.replaying:
                cached = self.resume_cache.results.get(key)
                if cached is None:
                    self.resume_cache.replaying = False
                    respawning = key in self.resume_cache.started
            self.run.meta["totals"]["agents"] = self._next_index
            self.run.save_meta()
            return index, key, cached, respawning

    def budget_state(self) -> dict[str, float | None]:
        return {"total": self.budget, "spent": self.spent}

    def _account_cost(
        self, cost: float | None, cost_estimated: bool, *, cached: bool
    ) -> None:
        if cost is None:
            return
        self.spent += cost
        totals = self.run.meta["totals"]
        totals["cost_estimated" if cost_estimated else "cost"] += cost
        if cached:
            totals["replayed_cost"] += cost

    def _register_process(self, proc: asyncio.subprocess.Process) -> None:
        self._processes.add(proc)

    async def agent(
        self,
        prompt: str,
        *,
        provider_name: str,
        model: str | None,
        effort: str | None,
        label: str,
        phase: str | None,
        cwd: str,
        cwd_key: str | None,
        schema: dict[str, Any] | None,
        isolation: str | None,
        timeout: float,
        stall_timeout: float | None,
        retries: int,
        web_search: bool,
        settings: str | None,
        sandbox: bool | None,
        env: dict[str, str] | None,
    ) -> str | None:
        if self.cancel_event.is_set():
            raise RuntimeError("FlowCancelled: workflow cancelled")
        provider = get_provider(provider_name)
        resolved_model = provider.resolve_model(model)
        index, key, cached, respawning = await self._assign_call(
            {
                "prompt": prompt,
                "provider": provider_name,
                "model": resolved_model,
                "effort": effort,
                "schema": schema,
                "cwd": cwd_key,
                "isolation": isolation,
            }
        )
        common = {
            "index": index,
            "key": key,
            "label": label,
            "provider": provider_name,
            "phase": phase,
        }
        if cached is not None:
            cost = cached.get("cost")
            cost_estimated = bool(cached.get("cost_estimated", False))
            self._account_cost(cost, cost_estimated, cached=True)
            result = cached.get("result")
            entry = {
                "type": "result",
                **common,
                "model": resolved_model,
                "result": result,
                "cost": cost,
                "cost_estimated": cost_estimated,
                "duration_ms": 0,
                "cached": True,
            }
            if "error" in cached:
                entry["error"] = cached["error"]
            self.run.journal(entry)
            totals = self.run.meta["totals"]
            totals["succeeded" if result is not None else "failed"] += 1
            self.run.save_meta()
            self._event(
                "cached",
                model=resolved_model,
                result=result,
                cost=cost,
                cost_estimated=cost_estimated,
                **common,
            )
            return result
        if self.resume_cache is not None and not self.resume_cache.replaying:
            if index == 0 or self.resume_cache.results or self.resume_cache.started:
                # Only the transition emits divergence; later live calls stay quiet.
                if not getattr(self, "_diverged", False):
                    self._diverged = True
                    self._event("diverged", index=index, key=key)
                    if respawning:
                        self._event(
                            "log",
                            message=f"respawning incomplete agent {index}: {label}",
                            phase=phase,
                        )
        self._event("queued", **common)
        log_path = self.run.agent_log_path(index, label)
        log_path.touch()
        started = time.monotonic()
        final_error = "unknown failure"
        total_cost: float | None = None
        result_message: str | None = None

        async with self.semaphore:
            if self.cancel_event.is_set():
                raise RuntimeError("FlowCancelled: workflow cancelled")
            for attempt in range(retries + 1):
                if self.cancel_event.is_set():
                    raise RuntimeError("FlowCancelled: workflow cancelled")
                self._event("started", attempt=attempt + 1, **common)
                self.run.journal(
                    {
                        "type": "started",
                        **common,
                        "model": resolved_model,
                        "attempt": attempt + 1,
                    }
                )

                def write_output(text: str) -> None:
                    with log_path.open("a") as handle:
                        handle.write(text)

                proc: asyncio.subprocess.Process | None = None

                def register(value: asyncio.subprocess.Process) -> None:
                    nonlocal proc
                    proc = value
                    self._register_process(value)

                exec_result: AsyncExecResult | None = None
                try:
                    command = provider.build_command(
                        prompt=prompt + FLOW_PROMPT_SUFFIX,
                        model=resolved_model,
                        reasoning_effort=effort,
                        web_search=web_search,
                        settings=settings,
                        sandbox=sandbox,
                        output_schema=None,
                    )
                    exec_result = await self.exec_fn(
                        command,
                        cwd,
                        timeout,
                        env,
                        on_output=write_output,
                        stall_timeout=stall_timeout,
                        on_process=register,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    final_error = f"execution failed: {exc}"
                finally:
                    if proc is not None:
                        self._processes.discard(proc)

                if self.cancel_event.is_set():
                    raise RuntimeError("FlowCancelled: workflow cancelled")
                if exec_result is None:
                    pass
                elif exec_result.timed_out:
                    final_error = f"timed out after {timeout:g}s"
                elif exec_result.stalled:
                    final_error = f"stalled for {stall_timeout:g}s"
                elif exec_result.exit_code != 0:
                    final_error = f"process exited {exec_result.exit_code}"
                else:
                    try:
                        agent_result = provider.parse_output(exec_result.stdout)
                    except Exception as exc:
                        final_error = f"output parse failed: {exc}"
                    else:
                        message = agent_result.result_message
                        if isinstance(message, str) and message:
                            result_message = message
                            total_cost = agent_result.total_cost
                            break
                        final_error = "provider returned no result"

                if attempt < retries:
                    delay = self.retry_delays[min(attempt, len(self.retry_delays) - 1)]
                    self._event(
                        "retry",
                        attempt=attempt + 1,
                        delay=delay,
                        error=final_error,
                        **common,
                    )
                    await self._backoff(delay)

        duration_ms = int((time.monotonic() - started) * 1000)
        succeeded = result_message is not None
        cost_estimated = succeeded and total_cost is None
        effective_cost = estimated_cost(provider_name) if cost_estimated else total_cost
        entry = {
            "type": "result",
            "key": key,
            "index": index,
            "label": label,
            "provider": provider_name,
            "model": resolved_model,
            "result": result_message,
            "cost": effective_cost,
            "cost_estimated": cost_estimated,
            "duration_ms": duration_ms,
        }
        if not succeeded:
            entry["error"] = final_error
        self.run.journal(entry)
        totals = self.run.meta["totals"]
        totals["succeeded" if succeeded else "failed"] += 1
        self._account_cost(effective_cost, cost_estimated, cached=False)
        self.run.save_meta()
        if succeeded:
            self._event(
                "done",
                duration_ms=duration_ms,
                cost=effective_cost,
                cost_estimated=cost_estimated,
                model=resolved_model,
                **common,
            )
        else:
            self._event("failed", duration_ms=duration_ms, error=final_error, **common)
        return result_message
