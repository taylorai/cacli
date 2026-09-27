"""Spawn coding agents in tmux sessions."""

import os
import shlex
import shutil
import subprocess
import time

from cacli.runner import build_launch, build_initial_log_entry
from cacli.cli import _provider_params
from cacli.runner import check_credentials
from cacli.sessions import (
    SessionInfo,
    ensure_sessions_dir,
    generate_session_id,
    save_session,
)


def _check_tmux() -> None:
    if shutil.which("tmux") is None:
        print("Error: tmux is required for 'cacli spawn'. Install it with:")
        print("  brew install tmux    # macOS")
        print("  apt install tmux     # Ubuntu/Debian")
        raise SystemExit(1)


def spawn_agent(args) -> None:
    """Spawn an agent in a detached tmux session."""
    _check_tmux()

    session_id = generate_session_id()
    tmux_name = f"cacli-{session_id}"
    sessions_dir = ensure_sessions_dir()
    log_file = str(sessions_dir / f"{session_id}.log")
    exitcode_file = str(sessions_dir / f"{session_id}.exitcode")
    cwd = os.path.abspath(args.cwd)

    # Build agent command using existing provider abstraction
    launch = build_launch(
        args.harness,
        args.prompt,
        provider=args.provider,
        params=_provider_params(),
        model=args.model,
        reasoning_effort=getattr(args, "reasoning_effort", None),
        web_search=getattr(args, "web_search", True),
        settings=getattr(args, "settings", None),
        sandbox=getattr(args, "sandbox", None),
    )

    check_credentials(launch)
    agent_cmd = launch.command

    # Seed the log with the prompt — provider stream output doesn't echo it
    initial_entry = build_initial_log_entry(args.harness, args.prompt, args.model)
    with open(log_file, "w") as f:
        f.write(initial_entry + "\n")

    # Wrapper: clean env, run agent with output logging, capture exit code
    # Unset CLAUDECODE so spawned claude agents don't think they're nested
    wrapper = (
        f"unset CLAUDECODE; "
        f"{{ {agent_cmd}; }} 2>&1 | tee -a {shlex.quote(log_file)}; "
        f"echo ${{PIPESTATUS[0]}} > {shlex.quote(exitcode_file)}"
    )

    # Save session metadata before spawning
    session = SessionInfo(
        id=session_id,
        prompt=args.prompt,
        harness=args.harness,
        provider=launch.provider,
        model=args.model,
        status="running",
        start_time=time.time(),
        end_time=None,
        tmux_session=tmux_name,
        log_file=log_file,
        cwd=cwd,
        command=agent_cmd,
        exit_code=None,
        name=getattr(args, "name", None),
    )
    save_session(session)

    # Spawn detached tmux session
    tmux_cmd = [
        "tmux",
        "new-session",
        "-d",
        "-s",
        tmux_name,
        "-c",
        cwd,
        "bash",
        "-c",
        wrapper,
    ]
    try:
        subprocess.run(tmux_cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        print(f"Error spawning tmux session: {e.stderr.decode().strip()}")
        raise SystemExit(1)

    print(f"Spawned session {session_id} (tmux: {tmux_name})")
    if args.harness:
        model_str = f" ({args.model})" if args.model else ""
        print(f"  Harness: {args.harness}; provider: {launch.provider}{model_str}")
    print(f"  Log: {log_file}")
    print(f"  Attach: tmux attach -t {tmux_name}")
    print("  Status: cacli status")
