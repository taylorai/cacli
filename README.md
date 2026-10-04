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

## Run an agent

A run selects a CLI harness, an inference provider, and an unqualified model ID:

```bash
cacli run "Review this diff" --harness claude --provider claude-api --model claude-opus-4-8
cacli run "Review this diff" --harness pi --provider workers-ai --model @cf/zai-org/glm-5.3
cacli run "Review this diff" --harness codex --provider azure --model gpt-5.3-codex
```

The CLI reads non-secret provider parameters from `AWS_REGION`,
`CLOUDFLARE_ACCOUNT_ID`, or `AZURE_OPENAI_ENDPOINT` and checks required credential names or login files
before execution. `build_launch()` returns the command, non-secret environment,
environment variables to unset, and credential wire metadata without reading
secret values. See [the provider design](docs/harnesses-and-providers.md).

## Workflows (`cacli flow`)

Flows are sandboxed Python scripts that orchestrate coding-agent subprocesses
across harnesses and inference providers. The orchestration script has no direct filesystem, network,
environment, or subprocess access; its side effects go through the injected
workflow API, while the agents it launches run with their normal permissions.

Create `review_flow.py`:

```python
META = {"name": "cross-review", "description": "cross-vendor review"}

async def main():
    phase("Review")
    reviews = await parallel([
        lambda: agent("Find correctness bugs in the current diff", harness="codex"),
        lambda: agent("Find security issues in the current diff", harness="claude"),
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

- Harness selection for Claude, Codex, Devin, Cursor, OpenCode, and Pi, with explicit inference providers
- Structured output validated against JSON Schema
- Journal-based resume that replays unchanged calls
- Dollar budgets with resumable budget-exceeded runs
- Sandboxed orchestration scripts with a focused async Python DSL

## Development

```bash
uv sync                     # installs pinned dev tools (ruff, ty, pytest, prek)
uv run prek install         # once: run the hooks on every commit
uv run prek run --all-files # lint, format, typecheck, and test the whole repo
```

Hooks come from `.pre-commit-config.yaml` and need prek; stock pre-commit
cannot run it. Tool versions are pinned in `pyproject.toml`'s dev group.
