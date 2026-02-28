"""Curses TUI dashboard for spawned agent sessions."""

import curses
import os
import subprocess
import time

from cacli.sessions import (
    delete_session,
    is_tmux_session_alive,
    list_sessions,
    save_session,
    sync_session_status,
)


def _format_runtime(start: float, end: float | None) -> str:
    elapsed = (end or time.time()) - start
    if elapsed < 60:
        return f"{int(elapsed)}s"
    elif elapsed < 3600:
        return f"{int(elapsed // 60)}m {int(elapsed % 60):02d}s"
    else:
        h = int(elapsed // 3600)
        m = int((elapsed % 3600) // 60)
        return f"{h}h {m:02d}m"


def status_tui() -> None:
    """Launch the curses-based status dashboard."""
    sessions = list_sessions()
    if not sessions:
        print("No sessions found. Use 'cacli spawn <prompt>' to start one.")
        return
    curses.wrapper(_tui_main)


def _init_curses(stdscr) -> None:
    """Set up curses state (colors, input mode, cursor)."""
    curses.curs_set(0)
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)  # running
    curses.init_pair(2, curses.COLOR_CYAN, -1)  # done
    curses.init_pair(3, curses.COLOR_RED, -1)  # failed
    curses.init_pair(4, curses.COLOR_YELLOW, -1)  # header accent
    curses.halfdelay(20)  # 2-second refresh
    curses.noecho()
    curses.cbreak()
    stdscr.keypad(True)


def _suspend_curses_and_run(cmd: list[str]) -> None:
    """Temporarily leave curses, run an external command, return."""
    curses.endwin()
    subprocess.run(cmd)


def _resume_curses() -> "curses.window":
    """Re-enter curses after a suspend."""
    stdscr = curses.initscr()
    _init_curses(stdscr)
    return stdscr


def _tui_main(stdscr) -> None:
    _init_curses(stdscr)

    selected = 0

    while True:
        stdscr.erase()
        height, width = stdscr.getmaxyx()

        # Sync and load sessions
        sessions = list_sessions()
        for i, s in enumerate(sessions):
            sessions[i] = sync_session_status(s)

        # Header
        title = "cacli status"
        keys_help = "[a]ttach [k]ill [l]og [d]elete [q]uit"
        stdscr.addnstr(0, 0, title, width - 1, curses.A_BOLD)
        if width > len(title) + len(keys_help) + 2:
            stdscr.addnstr(
                0, width - len(keys_help) - 1, keys_help, width - 1, curses.A_DIM
            )

        # Column headers
        id_w, prov_w, model_w, status_w, runtime_w = 10, 10, 18, 10, 10
        fixed_w = id_w + prov_w + model_w + status_w + runtime_w + 1
        col_header = (
            f" {'ID':<{id_w}}"
            f"{'Provider':<{prov_w}}"
            f"{'Model':<{model_w}}"
            f"{'Status':<{status_w}}"
            f"{'Runtime':<{runtime_w}}"
            f"Prompt"
        )
        stdscr.addnstr(2, 0, col_header, width - 1, curses.A_BOLD | curses.A_UNDERLINE)

        # Clamp selection
        if sessions:
            selected = max(0, min(selected, len(sessions) - 1))

        # Rows
        for i, session in enumerate(sessions):
            row_y = i + 3
            if row_y >= height - 1:
                break

            runtime = _format_runtime(session.start_time, session.end_time)
            prompt_max = max(0, width - fixed_w - 1)
            prompt_trunc = session.prompt[:prompt_max].replace("\n", " ")
            model_display = session.model or "default"

            is_selected = i == selected
            base_attr = curses.A_REVERSE if is_selected else curses.A_NORMAL

            # Build the row without status
            prefix = (
                f" {session.id:<{id_w}}"
                f"{session.provider:<{prov_w}}"
                f"{model_display:<{model_w}}"
            )
            suffix = f"{runtime:<{runtime_w}}{prompt_trunc}"

            # Write prefix
            stdscr.addnstr(row_y, 0, prefix, width - 1, base_attr)

            # Write status with color
            status_color = {
                "running": curses.color_pair(1),
                "done": curses.color_pair(2),
                "failed": curses.color_pair(3),
            }.get(session.status, curses.A_NORMAL)
            status_str = f"{session.status:<{status_w}}"
            col_offset = len(prefix)
            if col_offset + status_w < width:
                stdscr.addnstr(
                    row_y,
                    col_offset,
                    status_str,
                    min(status_w, width - col_offset - 1),
                    base_attr | status_color | curses.A_BOLD,
                )

            # Write suffix (runtime + prompt)
            col_offset += status_w
            if col_offset < width - 1:
                stdscr.addnstr(
                    row_y,
                    col_offset,
                    suffix,
                    width - col_offset - 1,
                    base_attr,
                )

        if not sessions:
            msg = "No sessions found. Use 'cacli spawn <prompt>' to start one."
            stdscr.addnstr(4, 2, msg, width - 3, curses.A_DIM)

        stdscr.refresh()

        # Handle input
        try:
            key = stdscr.getch()
        except curses.error:
            continue

        if key == -1:
            continue
        elif key == ord("q"):
            break
        elif key == curses.KEY_UP and selected > 0:
            selected -= 1
        elif key == curses.KEY_DOWN and sessions and selected < len(sessions) - 1:
            selected += 1
        elif key in (ord("a"), 10) and sessions:
            # Attach to tmux session
            s = sessions[selected]
            if is_tmux_session_alive(s.tmux_session):
                _suspend_curses_and_run(["tmux", "attach", "-t", s.tmux_session])
                stdscr = _resume_curses()
        elif key == ord("k") and sessions:
            # Kill running session
            s = sessions[selected]
            if s.status == "running":
                subprocess.run(
                    ["tmux", "kill-session", "-t", s.tmux_session],
                    capture_output=True,
                )
                s.status = "failed"
                s.end_time = time.time()
                save_session(s)
        elif key == ord("l") and sessions:
            # View log in pager
            s = sessions[selected]
            if os.path.exists(s.log_file):
                pager = os.environ.get("PAGER", "less")
                _suspend_curses_and_run([pager, s.log_file])
                stdscr = _resume_curses()
        elif key == ord("d") and sessions:
            # Delete finished session
            s = sessions[selected]
            if s.status != "running":
                delete_session(s.id)
                if selected > 0:
                    selected -= 1
