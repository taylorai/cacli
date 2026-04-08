"""Recover file changes from Claude Code or Codex transcripts."""

from __future__ import annotations

import difflib
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class TranscriptApplyError(Exception):
    """Raised when a transcript cannot be parsed or replayed."""


@dataclass
class TranscriptEntry:
    """Parsed transcript entry with source location metadata."""

    raw: dict[str, Any]
    source_ref: str


@dataclass
class FileState:
    """Virtual file contents used for dry-run planning."""

    exists: bool
    content: str


@dataclass
class TranscriptAction:
    """A reconstructable filesystem action extracted from a transcript."""

    index: int
    source_ref: str
    provider: str
    kind: str
    path: str | None
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    status: str = "pending"
    reason: str | None = None


@dataclass
class BashWarning:
    """A shell command that likely mutated files but cannot be replayed."""

    source_ref: str
    provider: str
    command: str
    reason: str
    blocking: bool = True


@dataclass
class TimelineEvent:
    """Ordered transcript event used during replay planning."""

    kind: str
    action: TranscriptAction | None = None
    warning: BashWarning | None = None


@dataclass
class TranscriptAnalysis:
    """Replay analysis against the current working tree."""

    provider: str
    transcript_path: Path
    root: Path
    total_entries: int
    skipped_lines: int
    actions: list[TranscriptAction]
    bash_warnings: list[BashWarning]
    applicable_actions: list[TranscriptAction]
    touched_paths: list[str]
    original_files: dict[str, FileState]
    final_files: dict[str, FileState]
    blocker: str | None

    @property
    def fully_applicable(self) -> bool:
        return self.blocker is None and len(self.applicable_actions) == len(self.actions)


class VirtualWorkspace:
    """Lazy virtual filesystem rooted at a working tree."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self._files: dict[str, FileState] = {}
        self._loaded: set[str] = set()
        self._touched: set[str] = set()

    def normalize_path(self, raw_path: str) -> str:
        path = Path(raw_path)
        if path.is_absolute():
            try:
                path = path.resolve().relative_to(self.root)
            except ValueError as exc:
                raise TranscriptApplyError(
                    f"path {raw_path!r} is outside the target root {self.root}"
                ) from exc
        normalized = path.as_posix()
        if normalized in {"", "."}:
            raise TranscriptApplyError("empty or root path is not a valid edit target")
        if normalized.startswith("../") or "/../" in f"/{normalized}":
            raise TranscriptApplyError(f"path {raw_path!r} escapes the target root")
        return normalized

    def get(self, rel_path: str) -> FileState:
        rel_path = self.normalize_path(rel_path)
        if rel_path not in self._loaded:
            abs_path = self.root / rel_path
            if abs_path.exists():
                if abs_path.is_dir():
                    raise TranscriptApplyError(f"path {rel_path!r} is a directory")
                content = abs_path.read_text(encoding="utf-8")
                self._files[rel_path] = FileState(exists=True, content=content)
            else:
                self._files[rel_path] = FileState(exists=False, content="")
            self._loaded.add(rel_path)
        return self._files[rel_path]

    def snapshot(self, rel_path: str) -> FileState:
        state = self.get(rel_path)
        return FileState(exists=state.exists, content=state.content)

    def set_file(self, rel_path: str, content: str) -> None:
        rel_path = self.normalize_path(rel_path)
        self._files[rel_path] = FileState(exists=True, content=content)
        self._loaded.add(rel_path)
        self._touched.add(rel_path)

    def delete_file(self, rel_path: str) -> None:
        rel_path = self.normalize_path(rel_path)
        self._files[rel_path] = FileState(exists=False, content="")
        self._loaded.add(rel_path)
        self._touched.add(rel_path)

    def move_file(self, old_path: str, new_path: str) -> None:
        old_path = self.normalize_path(old_path)
        new_path = self.normalize_path(new_path)
        state = self.snapshot(old_path)
        if not state.exists:
            raise TranscriptApplyError(f"cannot move missing file {old_path}")
        self.delete_file(old_path)
        self.set_file(new_path, state.content)
        self._touched.add(old_path)
        self._touched.add(new_path)

    def touched_paths(self) -> list[str]:
        return sorted(self._touched)

    def clone(self) -> "VirtualWorkspace":
        clone = VirtualWorkspace(self.root)
        clone._files = {
            path: FileState(exists=state.exists, content=state.content)
            for path, state in self._files.items()
        }
        clone._loaded = set(self._loaded)
        clone._touched = set(self._touched)
        return clone


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.startswith("{") or stripped.startswith("["):
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return value
    return value


def _coerce_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, indent=2, ensure_ascii=False)


def _tool_name(name: str | None) -> str:
    if not name:
        return ""
    return name.split(".")[-1].strip().lower().replace("-", "_")


def _looks_like_codex(entries: list[TranscriptEntry]) -> bool:
    return any(entry.raw.get("type", "").startswith("item.") for entry in entries)


def _looks_like_claude(entries: list[TranscriptEntry]) -> bool:
    for entry in entries:
        entry_type = entry.raw.get("type")
        if entry_type in {"assistant", "result", "system", "user"}:
            return True
        message = entry.raw.get("message")
        if isinstance(message, dict) and message.get("content"):
            return True
    return False


def load_transcript_entries(path: Path) -> tuple[list[TranscriptEntry], int]:
    """Load a transcript file as JSON or JSONL."""
    text = path.read_text(encoding="utf-8")
    stripped = text.strip()
    if not stripped:
        raise TranscriptApplyError(f"transcript {path} is empty")

    try:
        data = json.loads(stripped)
    except json.JSONDecodeError:
        entries: list[TranscriptEntry] = []
        skipped = 0
        for line_no, line in enumerate(text.splitlines(), start=1):
            candidate = line.strip()
            if not candidate:
                continue
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                skipped += 1
                continue
            if isinstance(data, list):
                for idx, item in enumerate(data, start=1):
                    if isinstance(item, dict):
                        entries.append(
                            TranscriptEntry(
                                raw=item,
                                source_ref=f"line {line_no} item {idx}",
                            )
                        )
            elif isinstance(data, dict):
                entries.append(TranscriptEntry(raw=data, source_ref=f"line {line_no}"))
        if not entries:
            raise TranscriptApplyError(
                f"transcript {path} does not contain JSON or JSONL entries"
            )
        return entries, skipped

    if isinstance(data, list):
        entries = [
            TranscriptEntry(raw=item, source_ref=f"entry {idx}")
            for idx, item in enumerate(data, start=1)
            if isinstance(item, dict)
        ]
    elif isinstance(data, dict):
        embedded = data.get("entries") or data.get("events") or data.get("transcript")
        if isinstance(embedded, list):
            entries = [
                TranscriptEntry(raw=item, source_ref=f"entry {idx}")
                for idx, item in enumerate(embedded, start=1)
                if isinstance(item, dict)
            ]
        else:
            entries = [TranscriptEntry(raw=data, source_ref="entry 1")]
    else:
        raise TranscriptApplyError(f"unsupported transcript format in {path}")

    if not entries:
        raise TranscriptApplyError(f"transcript {path} did not contain any object entries")
    return entries, 0


def detect_provider(entries: list[TranscriptEntry], requested: str | None = None) -> str:
    """Detect transcript provider from event shapes."""
    if requested and requested != "auto":
        return requested
    if _looks_like_codex(entries):
        return "codex"
    if _looks_like_claude(entries):
        return "claude"
    raise TranscriptApplyError(
        "could not detect transcript provider; use --provider {claude,codex}"
    )


def _new_action(
    actions: list[TranscriptAction],
    timeline: list[TimelineEvent],
    *,
    source_ref: str,
    provider: str,
    kind: str,
    path: str | None,
    summary: str,
    payload: dict[str, Any],
) -> None:
    action = TranscriptAction(
        index=len(actions) + 1,
        source_ref=source_ref,
        provider=provider,
        kind=kind,
        path=path,
        summary=summary,
        payload=payload,
    )
    actions.append(action)
    timeline.append(TimelineEvent(kind="action", action=action))


def _extract_patch_body(command: str) -> str | None:
    heredoc_match = re.search(
        r"apply_patch\s+<<['\"]?(?P<tag>[A-Za-z0-9_]+)['\"]?\n(?P<body>.*)\n(?P=tag)\s*$",
        command,
        re.DOTALL,
    )
    if heredoc_match:
        return heredoc_match.group("body")
    start = command.find("*** Begin Patch")
    end = command.rfind("*** End Patch")
    if start != -1 and end != -1:
        return command[start : end + len("*** End Patch")]
    return None


def _classify_shell_command(command: str) -> str | None:
    patterns = [
        ("sed -i in-place edit", r"\bsed\b[^\n]*\s-i(?:\b|[ =])"),
        ("perl in-place edit", r"\bperl\b[^\n]*\s-pi\b"),
        ("git apply or patch", r"\b(?:git\s+apply|patch)\b"),
        ("redirected file write", r"(?:^|[;&|]\s*)(?:cat|printf|echo|tee)\b[^\n]*(?:>|>>)\s*\S"),
        ("file move/copy/delete", r"\b(?:mv|cp|rm|install|truncate)\b"),
    ]
    for reason, pattern in patterns:
        if re.search(pattern, command):
            return reason
    return None


def _extract_shell_command(tool_name: str, tool_input: Any) -> str | None:
    normalized = _tool_name(tool_name)
    payload = _parse_jsonish(tool_input)
    if normalized in {"bash", "shell", "exec_command", "command_execution"}:
        if isinstance(payload, dict):
            command = payload.get("command") or payload.get("cmd")
            if isinstance(command, list):
                return " ".join(str(part) for part in command)
            if isinstance(command, str):
                return command
        if isinstance(payload, str):
            return payload
    return None


def _parse_apply_patch_text(
    patch_text: str,
    source_ref: str,
    provider: str,
    actions: list[TranscriptAction],
    timeline: list[TimelineEvent],
) -> None:
    lines = patch_text.splitlines()
    if not lines or lines[0].strip() != "*** Begin Patch":
        raise TranscriptApplyError(f"{source_ref}: invalid apply_patch payload")
    idx = 1
    while idx < len(lines):
        line = lines[idx]
        if line.strip() == "*** End Patch":
            return
        if line.startswith("*** Add File: "):
            path = line[len("*** Add File: ") :].strip()
            idx += 1
            content_lines: list[str] = []
            while idx < len(lines) and not lines[idx].startswith("*** "):
                if not lines[idx].startswith("+"):
                    raise TranscriptApplyError(
                        f"{source_ref}: invalid Add File patch line {lines[idx]!r}"
                    )
                content_lines.append(lines[idx][1:])
                idx += 1
            content = "\n".join(content_lines)
            if content_lines:
                content += "\n"
            _new_action(
                actions,
                timeline,
                source_ref=source_ref,
                provider=provider,
                kind="add",
                path=path,
                summary=f"add {path}",
                payload={"path": path, "content": content},
            )
            continue
        if line.startswith("*** Delete File: "):
            path = line[len("*** Delete File: ") :].strip()
            idx += 1
            _new_action(
                actions,
                timeline,
                source_ref=source_ref,
                provider=provider,
                kind="delete",
                path=path,
                summary=f"delete {path}",
                payload={"path": path},
            )
            continue
        if line.startswith("*** Update File: "):
            path = line[len("*** Update File: ") :].strip()
            idx += 1
            move_to = None
            if idx < len(lines) and lines[idx].startswith("*** Move to: "):
                move_to = lines[idx][len("*** Move to: ") :].strip()
                idx += 1
            hunk_lines: list[str] = []
            while idx < len(lines) and not lines[idx].startswith("*** "):
                hunk_lines.append(lines[idx])
                idx += 1
            _new_action(
                actions,
                timeline,
                source_ref=source_ref,
                provider=provider,
                kind="patch",
                path=path,
                summary=f"patch {path}" if not move_to else f"patch {path} -> {move_to}",
                payload={"path": path, "move_to": move_to, "hunks": hunk_lines},
            )
            continue
        raise TranscriptApplyError(f"{source_ref}: unsupported patch directive {line!r}")
    raise TranscriptApplyError(f"{source_ref}: apply_patch payload is missing *** End Patch")


def _extract_common_tool_actions(
    *,
    provider: str,
    source_ref: str,
    tool_name: str,
    tool_input: Any,
    actions: list[TranscriptAction],
    bash_warnings: list[BashWarning],
    timeline: list[TimelineEvent],
) -> None:
    normalized = _tool_name(tool_name)
    payload = _parse_jsonish(tool_input)

    if normalized == "write" and isinstance(payload, dict):
        path = payload.get("file_path") or payload.get("path")
        if isinstance(path, str):
            content = _coerce_text(payload.get("content"))
            _new_action(
                actions,
                timeline,
                source_ref=source_ref,
                provider=provider,
                kind="write",
                path=path,
                summary=f"write {path}",
                payload={"path": path, "content": content},
            )
        return

    if normalized in {"edit", "str_replace"} and isinstance(payload, dict):
        path = payload.get("file_path") or payload.get("path")
        old_text = payload.get("old_string") or payload.get("oldText") or payload.get(
            "before"
        )
        new_text = payload.get("new_string") or payload.get("newText") or payload.get(
            "after"
        )
        if isinstance(path, str) and old_text is not None and new_text is not None:
            _new_action(
                actions,
                timeline,
                source_ref=source_ref,
                provider=provider,
                kind="edit",
                path=path,
                summary=f"edit {path}",
                payload={
                    "path": path,
                    "old_string": _coerce_text(old_text),
                    "new_string": _coerce_text(new_text),
                    "replace_all": bool(
                        payload.get("replace_all") or payload.get("replaceAll")
                    ),
                },
            )
        return

    if normalized in {"multiedit", "multi_edit"} and isinstance(payload, dict):
        path = payload.get("file_path") or payload.get("path")
        edits = payload.get("edits")
        if isinstance(path, str) and isinstance(edits, list):
            normalized_edits = []
            for item in edits:
                if not isinstance(item, dict):
                    continue
                old_text = item.get("old_string") or item.get("oldText") or item.get(
                    "before"
                )
                new_text = item.get("new_string") or item.get("newText") or item.get(
                    "after"
                )
                if old_text is None or new_text is None:
                    continue
                normalized_edits.append(
                    {
                        "old_string": _coerce_text(old_text),
                        "new_string": _coerce_text(new_text),
                        "replace_all": bool(
                            item.get("replace_all") or item.get("replaceAll")
                        ),
                    }
                )
            if normalized_edits:
                _new_action(
                    actions,
                    timeline,
                    source_ref=source_ref,
                    provider=provider,
                    kind="multiedit",
                    path=path,
                    summary=f"multiedit {path}",
                    payload={"path": path, "edits": normalized_edits},
                )
        return

    if normalized == "apply_patch":
        if isinstance(payload, dict):
            patch_text = payload.get("patch") or payload.get("input")
        else:
            patch_text = payload
        if isinstance(patch_text, str):
            _parse_apply_patch_text(
                patch_text, source_ref, provider, actions, timeline
            )
        return

    shell_command = _extract_shell_command(normalized, payload)
    if shell_command:
        patch_text = _extract_patch_body(shell_command)
        if patch_text:
            _parse_apply_patch_text(
                patch_text, source_ref, provider, actions, timeline
            )
            return
        reason = _classify_shell_command(shell_command)
        if reason:
            warning = BashWarning(
                source_ref=source_ref,
                provider=provider,
                command=shell_command,
                reason=reason,
            )
            bash_warnings.append(warning)
            timeline.append(TimelineEvent(kind="warning", warning=warning))


def extract_actions_from_claude(
    entries: list[TranscriptEntry],
) -> tuple[list[TranscriptAction], list[BashWarning], list[TimelineEvent]]:
    """Extract reconstructable actions from Claude stream-json output."""
    actions: list[TranscriptAction] = []
    bash_warnings: list[BashWarning] = []
    timeline: list[TimelineEvent] = []

    for entry in entries:
        raw = entry.raw
        blocks: list[dict[str, Any]] = []
        if raw.get("type") == "tool_use":
            blocks = [raw]
        else:
            message = raw.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, list):
                    blocks = [item for item in content if isinstance(item, dict)]

        for block in blocks:
            if block.get("type") != "tool_use":
                continue
            _extract_common_tool_actions(
                provider="claude",
                source_ref=entry.source_ref,
                tool_name=str(block.get("name") or ""),
                tool_input=block.get("input"),
                actions=actions,
                bash_warnings=bash_warnings,
                timeline=timeline,
            )

    return actions, bash_warnings, timeline


def extract_actions_from_codex(
    entries: list[TranscriptEntry],
) -> tuple[list[TranscriptAction], list[BashWarning], list[TimelineEvent]]:
    """Extract reconstructable actions from Codex JSON event output."""
    actions: list[TranscriptAction] = []
    bash_warnings: list[BashWarning] = []
    timeline: list[TimelineEvent] = []

    for entry in entries:
        raw = entry.raw
        if raw.get("type") != "item.completed":
            continue
        item = raw.get("item")
        if not isinstance(item, dict):
            continue

        item_type = str(item.get("type") or "")
        if item_type == "command_execution":
            _extract_common_tool_actions(
                provider="codex",
                source_ref=entry.source_ref,
                tool_name="command_execution",
                tool_input=item,
                actions=actions,
                bash_warnings=bash_warnings,
                timeline=timeline,
            )
            continue

        if item_type not in {"tool_call", "function_call", "custom_tool_call"}:
            continue

        tool_name = (
            item.get("name")
            or item.get("tool")
            or item.get("function")
            or item.get("recipient_name")
        )
        tool_input = (
            item.get("arguments")
            or item.get("input")
            or item.get("args")
            or item.get("parameters")
        )
        _extract_common_tool_actions(
            provider="codex",
            source_ref=entry.source_ref,
            tool_name=str(tool_name or ""),
            tool_input=tool_input,
            actions=actions,
            bash_warnings=bash_warnings,
            timeline=timeline,
        )

    return actions, bash_warnings, timeline


def _apply_replace(
    content: str, old_text: str, new_text: str, replace_all: bool
) -> tuple[str, str | None]:
    if not old_text:
        return content, "empty old_string cannot be replayed safely"
    matches = content.count(old_text)
    if matches == 0:
        return content, "old_string was not found"
    if replace_all:
        return content.replace(old_text, new_text), None
    if matches > 1:
        return content, "old_string matched multiple locations"
    return content.replace(old_text, new_text, 1), None


def _find_unique_hunk_match(
    content_lines: list[str], before_lines: list[str], start_idx: int
) -> tuple[int, str | None]:
    if not before_lines:
        return start_idx, None

    matches: list[int] = []
    limit = len(content_lines) - len(before_lines) + 1
    for idx in range(max(start_idx, 0), max(limit, 0)):
        if content_lines[idx : idx + len(before_lines)] == before_lines:
            matches.append(idx)
    if len(matches) == 1:
        return matches[0], None

    if not matches:
        for idx in range(0, max(limit, 0)):
            if content_lines[idx : idx + len(before_lines)] == before_lines:
                matches.append(idx)
        if len(matches) == 1:
            return matches[0], None

    if not matches:
        return -1, "patch context was not found"
    return -1, "patch context matched multiple locations"


def _apply_patch_hunks(content: str, hunk_lines: list[str]) -> tuple[str, str | None]:
    content_lines = content.splitlines(keepends=True)
    chunks: list[list[str]] = []
    current: list[str] = []
    for line in hunk_lines:
        if line.startswith("@@"):
            if current:
                chunks.append(current)
            current = []
            continue
        current.append(line)
    if current:
        chunks.append(current)

    working = content_lines
    search_start = 0
    for chunk in chunks:
        before_lines: list[str] = []
        after_lines: list[str] = []
        for line in chunk:
            if not line:
                continue
            prefix = line[0]
            text = line[1:]
            if prefix in {" ", "-"}:
                before_lines.append(f"{text}\n")
            if prefix in {" ", "+"}:
                after_lines.append(f"{text}\n")
        match_idx, error = _find_unique_hunk_match(working, before_lines, search_start)
        if error:
            return content, error
        working = (
            working[:match_idx] + after_lines + working[match_idx + len(before_lines) :]
        )
        search_start = match_idx + len(after_lines)
    return "".join(working), None


def dry_run_action(
    workspace: VirtualWorkspace, action: TranscriptAction
) -> tuple[bool, str | None]:
    """Try to apply one action to the virtual workspace."""
    try:
        if action.kind == "write":
            path = workspace.normalize_path(str(action.payload["path"]))
            workspace.set_file(path, str(action.payload["content"]))
            return True, None

        if action.kind == "add":
            path = workspace.normalize_path(str(action.payload["path"]))
            state = workspace.snapshot(path)
            if state.exists:
                return False, "target file already exists"
            workspace.set_file(path, str(action.payload["content"]))
            return True, None

        if action.kind == "delete":
            path = workspace.normalize_path(str(action.payload["path"]))
            state = workspace.snapshot(path)
            if not state.exists:
                return False, "file does not exist"
            workspace.delete_file(path)
            return True, None

        if action.kind == "edit":
            path = workspace.normalize_path(str(action.payload["path"]))
            state = workspace.snapshot(path)
            if not state.exists:
                return False, "file does not exist"
            updated, error = _apply_replace(
                state.content,
                str(action.payload["old_string"]),
                str(action.payload["new_string"]),
                bool(action.payload.get("replace_all")),
            )
            if error:
                return False, error
            workspace.set_file(path, updated)
            return True, None

        if action.kind == "multiedit":
            path = workspace.normalize_path(str(action.payload["path"]))
            state = workspace.snapshot(path)
            if not state.exists:
                return False, "file does not exist"
            updated = state.content
            for edit in action.payload["edits"]:
                updated, error = _apply_replace(
                    updated,
                    str(edit["old_string"]),
                    str(edit["new_string"]),
                    bool(edit.get("replace_all")),
                )
                if error:
                    return False, error
            workspace.set_file(path, updated)
            return True, None

        if action.kind == "patch":
            path = workspace.normalize_path(str(action.payload["path"]))
            state = workspace.snapshot(path)
            if not state.exists:
                return False, "file does not exist"
            updated, error = _apply_patch_hunks(state.content, action.payload["hunks"])
            if error:
                return False, error
            move_to = action.payload.get("move_to")
            if move_to:
                new_path = workspace.normalize_path(str(move_to))
                target = workspace.snapshot(new_path)
                if target.exists and new_path != path:
                    return False, f"destination {new_path} already exists"
                workspace.delete_file(path)
                workspace.set_file(new_path, updated)
            else:
                workspace.set_file(path, updated)
            return True, None

    except TranscriptApplyError as exc:
        return False, str(exc)

    return False, f"unsupported action kind {action.kind}"


def _extract_states(
    root: Path, touched_paths: list[str], workspace: VirtualWorkspace
) -> tuple[dict[str, FileState], dict[str, FileState]]:
    original: dict[str, FileState] = {}
    final: dict[str, FileState] = {}
    for rel_path in touched_paths:
        abs_path = root / rel_path
        if abs_path.exists() and abs_path.is_file():
            original[rel_path] = FileState(
                exists=True, content=abs_path.read_text(encoding="utf-8")
            )
        else:
            original[rel_path] = FileState(exists=False, content="")
        final[rel_path] = workspace.snapshot(rel_path)
    return original, final


def analyze_transcript(
    transcript_path: str,
    *,
    cwd: str = ".",
    provider: str = "auto",
) -> TranscriptAnalysis:
    """Parse a transcript and plan which actions can be safely replayed."""
    transcript = Path(transcript_path).resolve()
    root = Path(cwd).resolve()
    entries, skipped_lines = load_transcript_entries(transcript)
    detected_provider = detect_provider(entries, provider)

    if detected_provider == "claude":
        actions, bash_warnings, timeline = extract_actions_from_claude(entries)
    elif detected_provider == "codex":
        actions, bash_warnings, timeline = extract_actions_from_codex(entries)
    else:
        raise TranscriptApplyError(f"unsupported provider {detected_provider}")

    workspace = VirtualWorkspace(root)
    applicable_actions: list[TranscriptAction] = []
    blocker: str | None = None

    future_blocker: str | None = None

    for event in timeline:
        if event.kind == "warning":
            warning = event.warning
            if warning and warning.blocking and future_blocker is None:
                future_blocker = (
                    f"{warning.source_ref}: unreconstructable shell command "
                    f"({warning.reason}) blocks later replay"
                )
            continue

        action = event.action
        if action is None:
            continue
        if future_blocker is not None and blocker is None:
            blocker = future_blocker
        if blocker is not None:
            action.status = "blocked"
            action.reason = blocker
            continue
        ok, reason = dry_run_action(workspace, action)
        if ok:
            action.status = "applicable"
            applicable_actions.append(action)
        else:
            blocker = f"{action.source_ref}: {action.summary} could not be replayed: {reason}"
            action.status = "conflict"
            action.reason = blocker

    touched_paths = workspace.touched_paths()
    original_files, final_files = _extract_states(root, touched_paths, workspace)
    return TranscriptAnalysis(
        provider=detected_provider,
        transcript_path=transcript,
        root=root,
        total_entries=len(entries),
        skipped_lines=skipped_lines,
        actions=actions,
        bash_warnings=bash_warnings,
        applicable_actions=applicable_actions,
        touched_paths=touched_paths,
        original_files=original_files,
        final_files=final_files,
        blocker=blocker,
    )


def _write_state(base: Path, rel_path: str, state: FileState) -> None:
    abs_path = base / rel_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    if state.exists:
        abs_path.write_text(state.content, encoding="utf-8")


def generate_patch(analysis: TranscriptAnalysis, patch_path: str | None = None) -> Path:
    """Write a git-style patch file for the auto-applicable action prefix."""
    output = Path(patch_path).resolve() if patch_path else (
        analysis.root / f"{analysis.transcript_path.stem}.recovered.patch"
    )
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix="cacli-transcript-") as temp_dir:
        parent = Path(temp_dir)
        before_dir = parent / "a"
        after_dir = parent / "b"
        before_dir.mkdir()
        after_dir.mkdir()

        for rel_path in analysis.touched_paths:
            _write_state(before_dir, rel_path, analysis.original_files[rel_path])
            _write_state(after_dir, rel_path, analysis.final_files[rel_path])

        result = subprocess.run(
            ["git", "diff", "--no-index", "--", "a", "b"],
            cwd=parent,
            capture_output=True,
            text=True,
        )
        if result.returncode not in {0, 1}:
            raise TranscriptApplyError(result.stderr.strip() or "git diff failed")
        patch = result.stdout
        if patch:
            patch = patch.replace("diff --git a/", "diff --git a/").replace(
                " b/", " b/"
            )
        output.write_text(patch, encoding="utf-8")
    return output


def apply_actions_to_disk(root: Path, actions: list[TranscriptAction]) -> None:
    """Apply a list of already-validated actions to disk."""
    workspace = VirtualWorkspace(root)
    for action in actions:
        ok, reason = dry_run_action(workspace, action)
        if not ok:
            raise TranscriptApplyError(reason or f"failed to apply {action.summary}")

    for rel_path in workspace.touched_paths():
        state = workspace.snapshot(rel_path)
        abs_path = root / rel_path
        if state.exists:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(state.content, encoding="utf-8")
        elif abs_path.exists():
            abs_path.unlink()


def _action_diff(root: Path, action: TranscriptAction) -> str:
    workspace = VirtualWorkspace(root)
    return _action_diff_for_workspace(workspace, action)


def _action_diff_for_workspace(
    workspace: VirtualWorkspace, action: TranscriptAction
) -> str:
    preview = workspace.clone()
    ok, reason = dry_run_action(preview, action)
    if not ok:
        return f"# cannot preview: {reason}"

    rel_path = action.path or str(action.payload.get("move_to") or action.payload.get("path"))
    if not rel_path:
        return action.summary
    rel_path = workspace.normalize_path(rel_path)
    before_state = workspace.snapshot(rel_path)
    after_state = preview.snapshot(rel_path)
    before = before_state.content if before_state.exists else ""
    after = after_state.content if after_state.exists else ""
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    )
    text = "".join(diff)
    if action.kind == "delete":
        return text or f"delete {rel_path}"
    if action.kind == "patch" and action.payload.get("move_to"):
        new_path = workspace.normalize_path(str(action.payload["move_to"]))
        return f"rename {rel_path} -> {new_path}\n{text}".strip()
    return text or action.summary


def run_interactive(analysis: TranscriptAnalysis) -> int:
    """Prompt to approve each auto-applicable action."""
    if not analysis.applicable_actions:
        print("No reconstructable transcript actions can be applied.")
        if analysis.blocker:
            print(f"Blocker: {analysis.blocker}")
        return 1

    approved: list[TranscriptAction] = []
    print(
        f"Provider: {analysis.provider} | applicable actions: "
        f"{len(analysis.applicable_actions)}/{len(analysis.actions)}"
    )
    if analysis.blocker:
        print(f"Replay stops at: {analysis.blocker}")
    if analysis.bash_warnings:
        print("Flagged mutating shell commands:")
        for warning in analysis.bash_warnings:
            print(f"  - {warning.source_ref}: {warning.reason}: {warning.command}")

    current = VirtualWorkspace(analysis.root)
    for action in analysis.applicable_actions:
        print()
        print(f"[{action.index}] {action.summary} ({action.source_ref})")
        preview = _action_diff_for_workspace(current, action).rstrip()
        if preview:
            print(preview)
        check = current.clone()
        ok, reason = dry_run_action(check, action)
        if not ok:
            print(f"Cannot apply in the current interactive state: {reason}")
            continue
        while True:
            response = input("Apply this change? [y]es/[n]o/[q]uit: ").strip().lower()
            if response in {"y", "yes"}:
                approved.append(action)
                current = check
                break
            if response in {"n", "no", ""}:
                break
            if response in {"q", "quit"}:
                if approved:
                    apply_actions_to_disk(analysis.root, approved)
                    print(f"Applied {len(approved)} transcript actions before quit.")
                    return 0
                print("Aborted without applying changes.")
                return 1

    if not approved:
        print("No transcript actions were approved.")
        return 1

    apply_actions_to_disk(analysis.root, approved)
    print(f"Applied {len(approved)} transcript actions.")
    return 0


def print_analysis_summary(analysis: TranscriptAnalysis) -> None:
    """Print a concise analysis summary before replay."""
    print(f"Transcript: {analysis.transcript_path}")
    print(f"Provider: {analysis.provider}")
    print(f"Parsed entries: {analysis.total_entries}")
    if analysis.skipped_lines:
        print(f"Skipped non-JSON lines: {analysis.skipped_lines}")
    print(f"Reconstructable actions: {len(analysis.actions)}")
    print(f"Auto-applicable actions: {len(analysis.applicable_actions)}")
    if analysis.blocker:
        print(f"Replay blocker: {analysis.blocker}")
    else:
        print("Replay blocker: none")
    if analysis.bash_warnings:
        print("Flagged mutating shell commands:")
        for warning in analysis.bash_warnings:
            print(f"  - {warning.source_ref}: {warning.reason}: {warning.command}")


def run_transcript_apply(args) -> int:
    """CLI entrypoint for transcript recovery."""
    analysis = analyze_transcript(
        args.transcript,
        cwd=args.cwd,
        provider=getattr(args, "provider", "auto"),
    )
    print_analysis_summary(analysis)

    mode = args.mode
    if mode == "generate-patch":
        patch_path = generate_patch(analysis, args.patch_file)
        print(f"Patch written to {patch_path}")
        if analysis.blocker:
            print("Patch only includes the auto-applicable prefix before the blocker.")
            return 1
        return 0

    if mode == "auto-apply":
        if not analysis.applicable_actions:
            print("No reconstructable transcript actions can be applied.")
            return 1
        apply_actions_to_disk(analysis.root, analysis.applicable_actions)
        print(f"Applied {len(analysis.applicable_actions)} transcript actions.")
        if analysis.blocker:
            print("Stopped before the blocker; later transcript changes were not applied.")
            return 1
        return 0

    if mode == "interactive":
        return run_interactive(analysis)

    raise TranscriptApplyError(f"unknown mode {mode}")
