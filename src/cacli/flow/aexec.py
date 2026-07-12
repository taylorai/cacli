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

# Provider final events are emitted at the end, so retain a bounded tail per
# stream. Truncation is exposed on AsyncExecResult rather than injected into
# stdout/stderr, which keeps provider parsers able to consume the retained tail.
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
READER_DRAIN_TIMEOUT = 3.0


@dataclass
class AsyncExecResult:
    """Captured result plus timeout diagnostics."""

    exit_code: int
    stdout: str
    stderr: str = ""
    timed_out: bool = False
    stalled: bool = False
    last_output_at: float | None = None
    stdout_truncated: bool = False
    stderr_truncated: bool = False


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
    stopped_at = time.monotonic()
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace)
    except asyncio.TimeoutError:
        pass
    remaining = grace - (time.monotonic() - stopped_at)
    if remaining > 0:
        await asyncio.sleep(remaining)
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if proc.returncode is None:
            proc.kill()
    await proc.wait()


async def _drain_readers(readers: list[asyncio.Task[None]]) -> None:
    try:
        await asyncio.wait_for(
            asyncio.gather(*readers, return_exceptions=True),
            timeout=READER_DRAIN_TIMEOUT,
        )
    except asyncio.TimeoutError:
        for task in readers:
            if not task.done():
                task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)


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
        # DEVNULL, not inherit: codex exec blocks forever reading a stdin that
        # never reaches EOF; headless agents must see a closed stdin.
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if on_process is not None:
        on_process(proc)

    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_truncated = False
    stderr_truncated = False
    started = time.monotonic()
    last_output_at = started

    async def read_stream(
        stream: asyncio.StreamReader | None, buffer: bytearray, stream_name: str
    ) -> None:
        nonlocal last_output_at, stdout_truncated, stderr_truncated
        if stream is None:
            return
        while True:
            data = await stream.read(4096)
            if not data:
                break
            text = data.decode(errors="replace")
            overflow = len(buffer) + len(data) - MAX_CAPTURE_BYTES
            if overflow > 0:
                if overflow >= len(buffer):
                    buffer.clear()
                else:
                    del buffer[:overflow]
                if stream_name == "stdout":
                    stdout_truncated = True
                else:
                    stderr_truncated = True
            buffer.extend(data[-MAX_CAPTURE_BYTES:])
            last_output_at = time.monotonic()
            await _emit(on_output, text)

    readers = [
        asyncio.create_task(read_stream(proc.stdout, stdout_buffer, "stdout")),
        asyncio.create_task(read_stream(proc.stderr, stderr_buffer, "stderr")),
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
        await _drain_readers(readers)
    except asyncio.CancelledError:
        await _stop_process(proc)
        await _drain_readers(readers)
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
        stdout=stdout_buffer.decode(errors="replace"),
        stderr=stderr_buffer.decode(errors="replace"),
        timed_out=timed_out,
        stalled=stalled,
        last_output_at=last_output_at,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
    )


async def terminate_process(
    proc: asyncio.subprocess.Process, grace: float = 10.0
) -> None:
    """Terminate a registered live agent process, then kill after ``grace``."""
    await _stop_process(proc, grace=grace)
