# cacli
coding agent CLI

## Recovering Changes From Transcripts

`cacli apply-transcript <transcript.jsonl>` can recover reconstructable file edits from Claude Code or Codex JSON/JSONL transcripts.

Examples:

```bash
# Produce a reviewable patch without touching the working tree
cacli apply-transcript session.jsonl --mode generate-patch

# Apply the auto-applicable prefix directly to the current tree
cacli apply-transcript session.jsonl --mode auto-apply

# Approve each recoverable edit interactively
cacli apply-transcript session.jsonl --mode interactive
```

Behavior:

- The command scans the full transcript before doing anything else.
- It reconstructs `Write`, `Edit`, `MultiEdit`, and `apply_patch` style changes.
- It flags mutating shell commands that cannot be reconstructed, such as `sed -i`.
- Automatic replay stops at the first unreconstructable mutating shell command or edit that no longer applies cleanly.
- `generate-patch` writes a patch for the auto-applicable prefix without modifying the working tree.
