"""Native async subprocess execution for workflow agents."""

from __future__ import annotations

import asyncio
import inspect
import os
import signal
import time
from dataclasses import dataclass
from typing import Awaitable, Callable


OutputCallback = Callable[[str], None | Awaitable[None]]
ProcessCallback = Callable[[asyncio.subprocess.Process], None]


@dataclass
class AsyncExecResult:
    """Captured result plus timeout diagnostics."""

    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    stalled: bool = False
    last_output_at: float | None = None


def clean_environment(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return the host environment without nested-agent guard variables."""
    environment = os.environ.copy()
    if extra:
        environment.update(extra)
    # Claude Code refuses nested invocations when this marker is present.
    environment.pop("CLAUDECODE", None)
    return environment


async def _emit(callback: OutputCallback | None, text: str) -> None:
    if callback is None:
        return
    value = callback(text)
    if inspect.isawaitable(value):
        await value


async def _stop_process(proc: asyncio.subprocess.Process, grace: float = 1.0) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        proc.kill()
    await proc.wait()


async def aexec(
    cmd: str,
    cwd: str,
    timeout: float,
    env: dict[str, str] | None = None,
    *,
    on_output: OutputCallback | None = None,
    stall_timeout: float | None = None,
    on_process: ProcessCallback | None = None,
) -> AsyncExecResult:
    """Run a shell command with incremental capture and hard wall-clock limits."""
    proc = await asyncio.create_subprocess_shell(
        cmd,
        cwd=cwd,
        env=clean_environment(env),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if on_process is not None:
        on_process(proc)

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    started = time.monotonic()
    last_output_at = started

    async def read_stream(
        stream: asyncio.StreamReader | None, chunks: list[str]
    ) -> None:
        nonlocal last_output_at
        if stream is None:
            return
        while True:
            data = await stream.read(4096)
            if not data:
                break
            text = data.decode(errors="replace")
            chunks.append(text)
            last_output_at = time.monotonic()
            await _emit(on_output, text)

    readers = [
        asyncio.create_task(read_stream(proc.stdout, stdout_chunks)),
        asyncio.create_task(read_stream(proc.stderr, stderr_chunks)),
    ]
    timed_out = False
    stalled = False
    try:
        while proc.returncode is None:
            now = time.monotonic()
            if now - started >= timeout:
                timed_out = True
                await _stop_process(proc)
                break
            if stall_timeout is not None and now - last_output_at >= stall_timeout:
                stalled = True
                await _stop_process(proc)
                break
            await asyncio.sleep(min(0.1, max(0.01, timeout / 100)))
        await proc.wait()
        await asyncio.gather(*readers)
    except asyncio.CancelledError:
        await _stop_process(proc)
        await asyncio.gather(*readers, return_exceptions=True)
        raise
    finally:
        for task in readers:
            if not task.done():
                task.cancel()

    exit_code = proc.returncode if proc.returncode is not None else 124
    if timed_out or stalled:
        exit_code = 124
    return AsyncExecResult(
        exit_code=exit_code,
        stdout="".join(stdout_chunks),
        stderr="".join(stderr_chunks),
        timed_out=timed_out,
        stalled=stalled,
        last_output_at=last_output_at,
    )


async def terminate_process(
    proc: asyncio.subprocess.Process, grace: float = 10.0
) -> None:
    """Terminate a registered live agent process, then kill after ``grace``."""
    await _stop_process(proc, grace=grace)
