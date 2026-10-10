"""Workflow agent-call lifecycle."""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Awaitable, Callable

from cacli.flow.aexec import AsyncExecResult, aexec, terminate_process
from cacli.flow.costs import estimated_cost
from cacli.flow.journal import JournalKeyChain, ResumeCache
from cacli.flow.runs import FlowRun
from cacli.flow.schema import (
    canonical_schema,
    decorate_prompt,
    extract_json,
    retry_prompt,
    schema_tier,
    strictify,
    validate_output,
)
from cacli.harnesses import get_harness
from cacli.inference import provider_params
from cacli.runner import build_launch, check_credentials


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
        self._agent_tasks: set[asyncio.Task[Any]] = set()
        self._closing = False
        self._cancel_task: asyncio.Task | None = None

    def start(self) -> None:
        if self._cancel_task is None:
            self._cancel_task = asyncio.create_task(self._watch_cancel())

    async def close(self) -> None:
        self._closing = True
        current = asyncio.current_task()
        pending_agents = [
            task
            for task in self._agent_tasks
            if task is not current and not task.done()
        ]
        for task in pending_agents:
            task.cancel()
        if pending_agents:
            await asyncio.gather(*pending_agents, return_exceptions=True)
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
        sleep_task = asyncio.ensure_future(self.sleep(delay))
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

    async def agent(self, prompt: str, **kwargs: Any) -> Any | None:
        """Run one tracked eager agent call.

        Monty schedules external async functions eagerly, including calls the
        workflow never awaits. Tracking the hosting task lets shutdown cancel
        that orphaned work before the run receives a terminal status.
        """
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("agent call is not running in an asyncio task")
        if self._closing:
            raise asyncio.CancelledError
        self._agent_tasks.add(task)
        try:
            return await self._run_agent(prompt, **kwargs)
        finally:
            self._agent_tasks.discard(task)

    async def _run_agent(
        self,
        prompt: str,
        *,
        harness_name: str,
        provider_name: str | None,
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
        schema_retries: int,
        web_search: bool,
        settings: str | None,
        sandbox: bool | None,
        env: dict[str, str] | None,
        extensions: tuple[str, ...] = (),
        trust_project: bool = True,
    ) -> Any | None:
        if self.cancel_event.is_set():
            raise RuntimeError("FlowCancelled: workflow cancelled")
        launch = build_launch(
            harness_name,
            prompt,
            provider=provider_name,
            model=model,
            params=provider_params(env),
            extensions=extensions,
            trust_project=trust_project,
        )
        adapter = get_harness(harness_name)
        if self.exec_fn is aexec:
            check_credentials(launch, env)
        provider_name = launch.provider
        resolved_model = launch.model
        # Pi-only options join the identity only when set, so resume keys of
        # runs recorded before these options existed stay valid.
        pi_options: dict[str, Any] = {}
        if extensions:
            pi_options["extensions"] = list(extensions)
        if not trust_project:
            pi_options["trust_project"] = False
        index, key, cached, respawning = await self._assign_call(
            {
                **pi_options,
                "prompt": prompt,
                "harness": harness_name,
                "provider": provider_name,
                "model": resolved_model,
                "effort": effort,
                "schema": schema,
                "cwd": cwd_key,
                "isolation": isolation,
                "web_search": web_search,
                "settings": settings,
                "sandbox": sandbox,
                "env": env,
                "retries": retries,
                "schema_retries": schema_retries,
                "timeout": timeout,
                "stall_timeout": stall_timeout,
            }
        )
        common = {
            "index": index,
            "key": key,
            "label": label,
            "harness": harness_name,
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
        tier = schema_tier(harness_name) if schema is not None else None
        schema_path = None
        if tier == "native-file":
            schema_path = self.run.path / "agents" / f"{index:04d}.schema.json"
            schema_path.write_text(canonical_schema(strictify(schema)))
        started = time.monotonic()
        final_error = "unknown failure"
        total_cost: float | None = None
        final_result: Any | None = None
        last_candidate = ""
        process_attempt = 0

        try:
            async with self.semaphore:
                if self.cancel_event.is_set():
                    raise RuntimeError("FlowCancelled: workflow cancelled")
                if self.budget is not None and self.spent >= self.budget:
                    raise RuntimeError(
                        f"FlowBudgetExceeded: spent ${self.spent:.2f} of "
                        f"${self.budget:.2f} budget"
                    )
                schema_attempts = schema_retries + 1 if schema is not None else 1
                decorated = (
                    decorate_prompt(prompt, schema) if schema is not None else prompt
                )
                schema_prompt = decorated if tier == "prompt" else prompt
                for schema_attempt in range(schema_attempts):
                    response_text = ""
                    agent_result = None
                    for exec_attempt in range(retries + 1):
                        agent_result = None
                        if self.cancel_event.is_set():
                            raise RuntimeError("FlowCancelled: workflow cancelled")
                        process_attempt += 1
                        self._event(
                            "started",
                            attempt=process_attempt,
                            schema_attempt=schema_attempt + 1,
                            **common,
                        )
                        self.run.journal(
                            {
                                "type": "started",
                                **common,
                                "model": resolved_model,
                                "attempt": process_attempt,
                                "schema_attempt": schema_attempt + 1,
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
                            output_schema = None
                            if tier == "native":
                                output_schema = schema
                            elif tier == "native-file":
                                output_schema = schema_path
                            attempt_launch = build_launch(
                                harness_name,
                                schema_prompt + FLOW_PROMPT_SUFFIX,
                                provider=provider_name,
                                model=model,
                                params=provider_params(env),
                                reasoning_effort=effort,
                                web_search=web_search,
                                settings=settings,
                                sandbox=sandbox,
                                output_schema=output_schema,
                                extensions=extensions,
                                trust_project=trust_project,
                            )
                            exec_result = await self.exec_fn(
                                attempt_launch.command,
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
                                agent_result = adapter.parse_output(exec_result.stdout)
                            except Exception as exc:
                                final_error = f"output parse failed: {exc}"
                            else:
                                response_text = agent_result.result_message
                                if agent_result.total_cost is not None:
                                    total_cost = (
                                        total_cost or 0.0
                                    ) + agent_result.total_cost
                                if agent_result.exit_code != 0:
                                    final_error = response_text or (
                                        f"harness exited {agent_result.exit_code}"
                                    )
                                    agent_result = None
                                    response_text = ""
                                elif isinstance(response_text, str) and response_text:
                                    break
                                elif (
                                    schema is not None
                                    and agent_result.structured_output is not None
                                ):
                                    break
                                else:
                                    final_error = "harness returned no result"

                        if exec_attempt < retries:
                            delay = self.retry_delays[
                                min(exec_attempt, len(self.retry_delays) - 1)
                            ]
                            self._event(
                                "retry",
                                attempt=process_attempt,
                                delay=delay,
                                error=final_error,
                                **common,
                            )
                            await self._backoff(delay)

                    if agent_result is None:
                        break
                    if schema is None:
                        final_result = response_text
                        break

                    candidate = agent_result.structured_output
                    if candidate is None:
                        candidate = extract_json(response_text)
                        last_candidate = response_text
                    else:
                        last_candidate = json.dumps(
                            candidate, sort_keys=True, separators=(",", ":")
                        )
                    if candidate is None:
                        final_error = "could not extract a JSON object or array"
                    else:
                        validation_error = validate_output(candidate, schema)
                        if validation_error is None:
                            final_result = candidate
                            break
                        final_error = validation_error

                    if schema_attempt < schema_retries:
                        self._event(
                            "schema_retry",
                            attempt=schema_attempt + 1,
                            error=final_error,
                            **common,
                        )
                        schema_prompt = retry_prompt(
                            decorated, final_error, last_candidate
                        )
                if (
                    schema is not None
                    and final_result is None
                    and agent_result is not None
                ):
                    final_error = "schema_exhausted"
        finally:
            if schema_path is not None:
                schema_path.unlink(missing_ok=True)

        duration_ms = int((time.monotonic() - started) * 1000)
        succeeded = final_result is not None
        cost_estimated = succeeded and total_cost is None
        effective_cost = estimated_cost(harness_name) if cost_estimated else total_cost
        entry = {
            "type": "result",
            "key": key,
            "index": index,
            "label": label,
            "harness": harness_name,
            "provider": provider_name,
            "model": resolved_model,
            "result": final_result,
            "cost": effective_cost,
            "cost_estimated": cost_estimated,
            "duration_ms": duration_ms,
        }
        if not succeeded:
            entry["error"] = final_error
            if final_error == "schema_exhausted":
                entry["last_candidate"] = last_candidate[:1000]
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
        return final_result
