"""Trusted Monty-side workflow DSL.

Monty 0.0.18 cannot define classes or resolve forward module references, so the
shim is a prelude and the user-facing API is deliberately flat. Cancellation
uses the built-in ``RuntimeError`` message prefix ``FlowCancelled:`` because
custom host exception identity is not preserved across the Monty boundary.

``agent`` is a direct alias of the ``_ctx_agent`` external function rather than
a sandbox-side wrapper: Monty starts external calls eagerly (JS-promise style),
so ``t = agent(...); ...; await t`` runs concurrently with other pending calls.
A sandbox ``async def`` wrapper would be lazy (CPython-coroutine style) and
silently serialize that pattern. Consequence: every ``agent()`` argument is
keyword-only after ``prompt``, and calling ``agent()`` starts the agent even if
the result is never awaited.
"""

PRELUDE = """import asyncio

flow_args = _ctx_args
agent = _ctx_agent
budget_total = _ctx_budget_total
budget_spent = _ctx_budget_spent
budget_remaining = _ctx_budget_remaining

def _flow_fatal(exc):
    message = str(exc)
    return (message.startswith("FlowCancelled:")
            or message.startswith("FlowAgentCapError:")
            or message.startswith("FlowBudgetExceeded:"))

def phase(title):
    return _ctx_phase(title)

def log(message):
    return _ctx_log(message)

async def parallel(thunks):
    if len(thunks) > 4096:
        raise ValueError("parallel supports at most 4096 items")
    async def run_one(thunk):
        try:
            return await thunk()
        except Exception as exc:
            if _flow_fatal(exc):
                raise
            _ctx_log(f"parallel branch failed: {exc}")
            return None
    return await asyncio.gather(*[run_one(thunk) for thunk in thunks])

async def pipeline(items, *stages):
    if len(items) > 4096:
        raise ValueError("pipeline supports at most 4096 items")
    async def run_one(item, index):
        result = item
        for stage in stages:
            try:
                result = await stage(result, item, index)
            except Exception as exc:
                if _flow_fatal(exc):
                    raise
                _ctx_log(f"pipeline item {index} failed: {exc}")
                return None
            if result is None:
                return None
        return result
    return await asyncio.gather(*[run_one(item, i) for i, item in enumerate(items)])
"""

EPILOGUE = """
_result = asyncio.run(main())
_result
"""

PRELUDE_LINE_COUNT = PRELUDE.count("\n")
