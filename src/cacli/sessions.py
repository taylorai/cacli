"""Session persistence and state tracking for spawned agents."""

import json
import subprocess
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path


SESSIONS_DIR = Path.home() / ".cacli" / "sessions"


@dataclass
class SessionInfo:
    """Metadata for a spawned agent session."""

    id: str
    prompt: str
    provider: str
    model: str | None
    status: str  # "running", "done", "failed"
    start_time: float
    end_time: float | None
    tmux_session: str
    log_file: str
    cwd: str
    command: str
    exit_code: int | None
    name: str | None = None


def ensure_sessions_dir() -> Path:
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return SESSIONS_DIR


def generate_session_id() -> str:
    return uuid.uuid4().hex[:8]


def save_session(session: SessionInfo) -> Path:
    ensure_sessions_dir()
    path = SESSIONS_DIR / f"{session.id}.json"
    path.write_text(json.dumps(asdict(session), indent=2))
    return path


def load_session(session_id: str) -> SessionInfo | None:
    path = SESSIONS_DIR / f"{session_id}.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return SessionInfo(**data)
    except (json.JSONDecodeError, TypeError):
        return None


def list_sessions() -> list[SessionInfo]:
    ensure_sessions_dir()
    sessions = []
    for p in SESSIONS_DIR.glob("*.json"):
        try:
            data = json.loads(p.read_text())
            sessions.append(SessionInfo(**data))
        except (json.JSONDecodeError, TypeError):
            continue
    sessions.sort(key=lambda s: s.start_time, reverse=True)
    return sessions


def delete_session(session_id: str) -> None:
    json_path = SESSIONS_DIR / f"{session_id}.json"
    log_path = SESSIONS_DIR / f"{session_id}.log"
    exitcode_path = SESSIONS_DIR / f"{session_id}.exitcode"
    json_path.unlink(missing_ok=True)
    log_path.unlink(missing_ok=True)
    exitcode_path.unlink(missing_ok=True)


def is_tmux_session_alive(session_name: str) -> bool:
    result = subprocess.run(
        ["tmux", "has-session", "-t", session_name],
        capture_output=True,
    )
    return result.returncode == 0


def sync_session_status(session: SessionInfo) -> SessionInfo:
    """Check tmux liveness and update session status if finished."""
    if session.status != "running":
        return session

    if is_tmux_session_alive(session.tmux_session):
        return session

    # Session ended — read exit code
    exitcode_path = SESSIONS_DIR / f"{session.id}.exitcode"
    exit_code = None
    if exitcode_path.exists():
        try:
            exit_code = int(exitcode_path.read_text().strip())
        except (ValueError, OSError):
            pass

    session.status = "done" if exit_code == 0 else "failed"
    session.exit_code = exit_code
    session.end_time = time.time()
    save_session(session)
    return session
