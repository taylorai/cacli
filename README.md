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

## Workflows (`cacli flow`)

Flows are sandboxed Python scripts that orchestrate coding-agent subprocesses
across providers. The orchestration script has no direct filesystem, network,
environment, or subprocess access; its side effects go through the injected
workflow API, while the agents it launches run with their normal permissions.

Create `review_flow.py`:

```python
META = {"name": "cross-review", "description": "cross-vendor review"}

async def main():
    phase("Review")
    reviews = await parallel([
        lambda: agent("Find correctness bugs in the current diff", provider="codex"),
        lambda: agent("Find security issues in the current diff", provider="claude"),
    ])
    log(f"Received {len([r for r in reviews if r])} reviews")
    return {"reviews": reviews}
```

Then run it with a dollar cap:

```bash
cacli flow run review_flow.py --budget 5.00
```

Useful commands:

```bash
cacli flow run flow.py [--args JSON] [--budget 10.00] [--resume RUN_ID]
cacli flow runs
cacli flow show RUN_ID
cacli flow docs | less
cacli flow install-skill
```

`cacli flow docs` prints the complete DSL reference. Claude users can install
the same reference as an agent skill with `cacli flow install-skill`.

Headline features:

- Cross-provider `agent()` calls to Claude, Codex, Devin, Cursor, Gemini, OpenCode, and Pi
- Structured output validated against JSON Schema
- Journal-based resume that replays unchanged calls
- Dollar budgets with resumable budget-exceeded runs
- Sandboxed orchestration scripts with a focused async Python DSL
