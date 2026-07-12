# Local CLI capability report

> **Historical snapshot (2026-07-11, pre-implementation).** The "cacli sandbox mapping today" column and gap notes describe the state BEFORE flow Phase 3 landed; the recommendations in this report have since been implemented (claude settings-based sandbox + --json-schema, cursor --sandbox mapping, codex --output-schema). See src/cacli/providers/ for current behavior.

Research was limited to local binaries, help/config inspection, and cacli source. No agent sessions were launched and no files were modified.

## Claude Code

**Version:** `2.1.207 (Claude Code)`  
**Binary:** `/Users/benjamin/.local/bin/claude`

### Structured output

Claude has native JSON-Schema-constrained output.

```text
--json-schema <schema>  JSON Schema for structured output validation. Example:
                        {"type":"object","properties":{"name":{"type":"string"}},"required":["name"]}
```

- Flag: `--json-schema <schema>`
- Input: inline JSON Schema, not a schema-file path.
- Intended for noninteractive `--print` operation.
- In JSON/stream-JSON output, the validated value is exposed as the `structured_output` property of the final `type: "result"` object/event. The ordinary textual final response remains in `result`.
- This differs from Codex: Claude accepts inline JSON; Codex accepts a path.

Claude also has two unconstrained JSON modes:

```text
--output-format <format>  Output format (only works with --print):
                          "text" (default), "json" (single result), or
                          "stream-json" (realtime streaming)
```

- `--output-format json`: one JSON result object on stdout. Its final text is in `result`; it also contains run metadata such as session/cost/error fields.
- `--output-format stream-json`: JSONL events on stdout. The terminal event has `type: "result"` and the final text in `result`.
- `--include-partial-messages` and `--include-hook-events` add events only in stream-JSON mode.

### Sandbox and permissions

There is no top-level `--sandbox` CLI flag. Claude’s OS-level sandbox is configured through settings, supplied using:

```text
--settings <file-or-json>  Path to a settings JSON file or a JSON string
```

The native binary contains the sandbox settings and macOS implementation:

- `sandbox.enabled`
- `sandbox.autoAllowBashIfSandboxed`
- `sandbox.excludedCommands`
- `sandbox.enableWeakerNestedSandbox`
- `sandbox.network.allowedDomains`
- `sandbox.network.allowManagedDomainsOnly`
- `sandbox.network.allowUnixSockets`
- filesystem read/write controls
- fail-closed configuration for unsupported/missing sandbox dependencies

On macOS it invokes `/usr/bin/sandbox-exec` (Seatbelt). The binary states:

> “All bash commands invoked by the model must run in the sandbox unless they are explicitly listed in excludedCommands.”

Relevant permission controls, distinct from OS isolation:

```text
--permission-mode <mode>
  acceptEdits | auto | bypassPermissions | manual | dontAsk | plan

--allowedTools / --allowed-tools <tools...>
--disallowedTools / --disallowed-tools <tools...>
--tools <tools...>
--dangerously-skip-permissions
--allow-dangerously-skip-permissions
--add-dir <directories...>
```

`--dangerously-skip-permissions` bypasses permission checks; it is not itself a sandbox-off flag. `plan` and restrictive tool lists limit behavior but are not substitutes for Seatbelt isolation.

### What cacli does today

`ClaudeProvider.build_command()` requests:

```text
claude -p ... --verbose --output-format=stream-json
```

`parse_output()` scans JSONL backward for the final `{"type":"result", ...}` event and extracts:

- `result` → `result_message`
- `total_cost_usd`
- `permission_denials`

The PR-description path requests single-object `--output-format json` and extracts its `result`.

Current gaps:

- `output_schema` is ignored despite native `--json-schema`.
- `sandbox` is ignored.
- No Claude sandbox settings are added for any of `True`, `False`, or `None`.

### Recommendation

| Unified value | Recommended Claude mapping |
|---|---|
| `sandbox=True` | Add invocation-local settings with `sandbox.enabled=true`, ideally through a generated settings file or carefully merged `--settings` JSON. Fail clearly if the installed Claude version/platform cannot establish the sandbox. |
| `sandbox=False` | Add invocation-local `sandbox.enabled=false`; do not use `--dangerously-skip-permissions` merely to disable OS isolation. |
| `sandbox=None` | Do not override sandbox settings; preserve Claude’s user/admin/provider default. |

For structured flow output, pass the flow schema inline through `--json-schema '<JSON>'`, retain `stream-json`, and read `structured_output` from the final result event.

---

## Codex CLI

**Version:** `codex-cli 0.144.0`  
**Binary launcher:** `/Users/benjamin/Library/pnpm/codex`

### Structured output

Codex has native JSON-Schema-constrained output:

```text
--output-schema <FILE>
    Path to a JSON Schema file describing the model's final response shape
```

- Flag: `codex exec --output-schema <FILE>`
- Input: filesystem path to a JSON Schema document.
- Codex does not accept inline schema JSON through this flag.
- The constrained final answer still appears as the final agent message:
  - in normal mode, as final plain text on stdout;
  - with `--json`, as the `text` of an `item.completed` event whose `item.type` is `agent_message`;
  - with `--output-last-message <FILE>`, in that file.
- The text is JSON conforming to the supplied schema; Codex does not document a separate `structured_output` event property.

Unconstrained JSON mode:

```text
--json
    Print events to stdout as JSONL

-o, --output-last-message <FILE>
    Specifies file where the last message from the agent should be written
```

`--json` emits JSONL lifecycle events. cacli’s relevant final shape is:

```json
{
  "type": "item.completed",
  "item": {
    "type": "agent_message",
    "text": "final response"
  }
}
```

Without `--json`, the noninteractive final message is plain text on stdout.

### Sandbox and permissions

Both top-level Codex and `codex exec` expose:

```text
-s, --sandbox <SANDBOX_MODE>
    [possible values: read-only, workspace-write, danger-full-access]

--dangerously-bypass-approvals-and-sandbox
    Skip all confirmation prompts and execute commands without sandboxing

-a, --ask-for-approval <APPROVAL_POLICY>
    untrusted | on-request | never

--add-dir <DIR>
```

Additional configuration can be passed with `-c key=value`. The local config includes `[sandbox_workspace_write]`, and Codex supports settings such as:

```text
-c sandbox_workspace_write.network_access=true
-c 'sandbox_permissions=["disk-full-read-access"]'
```

### What cacli does today

Every normal and PR-description command uses `--json --skip-git-repo-check`.

`parse_output()` scans JSONL backward for the final `item.completed` / `agent_message` and returns `item.text`.

Current sandbox mapping in `_sandbox_args()`:

| cacli value | Current command |
|---|---|
| `True` | `--sandbox workspace-write --config sandbox_workspace_write.network_access=true` |
| `False` | `--sandbox danger-full-access` |
| `None` | Converted to `CODEX_DEFAULT_SANDBOX=True`, therefore the same as `True` |

Current gap: `output_schema` is ignored even though Codex supports `--output-schema`.

### Recommendation

| Unified value | Recommended Codex mapping |
|---|---|
| `sandbox=True` | `--sandbox workspace-write`; explicitly decide whether flow semantics require the current `network_access=true` override. |
| `sandbox=False` | `--sandbox danger-full-access`. Do not use `--dangerously-bypass-approvals-and-sandbox` unless approval bypass is separately intended. |
| `sandbox=None` | Omit `--sandbox` and sandbox-related config so the Codex profile/config default applies. |

The current `None → True` behavior does not mean “provider default”; it is a cacli override. For structured flows, write the schema to a temporary JSON file, pass `--output-schema <file>`, and decode the final `agent_message.text` as JSON.

---

## Devin CLI

**Version:** `devin 3000.1.23 (13fc088b)`  
**Binary:** `/Users/benjamin/.local/bin/devin`

### Structured output

No native output-schema or JSON-schema flag was found.

No `--output-format`, `--json`, or JSONL stdout mode was found in top-level or sandbox subcommand help.

Noninteractive mode is:

```text
-p, --print [<PROMPT>]
    Print response and exit
```

Its stdout final response is plain text.

Devin also supports:

```text
--export [<PATH>]
    Export conversation to a file
    Exports after each turn.
```

That export is conversation/trajectory data, not a schema-constrained final-output facility and not a documented stdout JSON mode.

### Sandbox and permissions

Devin exposes a native OS-level sandbox:

```text
--sandbox
    [Research Preview] Sandbox exec-tool processes
    (macOS seatbelt / Linux bwrap+seccomp)

    When passed, the sandbox enforces the active Read/Write permission
    scopes at the OS level. Writable roots come from granted Write(...)
    scopes, readable roots come from granted Read(...) scopes.
```

Platform requirements from `devin sandbox setup --help`:

> “On Linux, `--sandbox` requires bubblewrap (bwrap) and socat. macOS sandboxing (seatbelt) works out of the box. On Windows, install and run Devin inside WSL 2 to use sandbox mode.”

There is no `--sandbox disabled` or `--no-sandbox` flag in this version; disabling means omitting `--sandbox`.

Permission modes:

```text
--permission-mode <PERMISSION_MODE>

auto          auto-approves read-only tools
accept-edits  also auto-approves workspace edits
smart         additionally auto-runs actions a fast model judges safe
dangerous     auto-approves all tools
```

The sandbox and permission mode are orthogonal: `dangerous` auto-approves tools, while `--sandbox` still confines exec processes at the OS level.

`--agent-config <FILE>` can define tool visibility and permissions with strict JSON/YAML parsing.

### What cacli does today

The bare command is built as:

```text
devin [--sandbox] --permission-mode dangerous [--model ...] --print ...
```

Normal runs are wrapped by `cacli devin-stream`, which tails a temporary Devin `--export` file and converts it into Claude-style stream JSON. PR-description runs skip the wrapper and consume plain stdout.

`parse_output()` recursively searches JSON/JSONL for text under:

- `result`
- `result_message`
- `text`
- `content`
- `message`

If the output is not JSON, it returns stripped plain text.

Current sandbox mapping:

| cacli value | Current command |
|---|---|
| `True` | Adds `--sandbox` |
| `False` | Omits `--sandbox` |
| `None` | Converted to `DEVIN_DEFAULT_SANDBOX=True`, so adds `--sandbox` |

`output_schema` is ignored because Devin has no native equivalent.

### Recommendation

| Unified value | Recommended Devin mapping |
|---|---|
| `sandbox=True` | Add `--sandbox`; allow Devin’s fail-closed startup error to surface clearly if prerequisites are unavailable. |
| `sandbox=False` | Omit `--sandbox`. |
| `sandbox=None` | Omit `--sandbox` so Devin’s environment/config default applies, including `DEVIN_SANDBOX` if set. |

If the product intentionally wants “secure by default,” that should be a documented flow-engine default resolved before provider dispatch, not an implicit reinterpretation of `None`.

Structured-output flows cannot get native schema enforcement from this Devin version. Either reject schema-required Devin steps or use prompt-plus-post-validation with clearly weaker guarantees.

---

## Cursor Agent

**Version:** `2026.05.28-a70ca7c`  
**Equivalent binaries:**

- `/Users/benjamin/.local/bin/cursor-agent`
- `/Users/benjamin/.local/bin/agent`
- `/Users/benjamin/.local/bin/cursor`

`agent` and `cursor-agent` resolve to the same executable. `cursor --version` reports that no Cursor IDE is installed and directs the user to `cursor agent` or `agent`; in this installation, the actual headless-agent entry points are `agent`/`cursor-agent`.

### Structured output

No native JSON Schema or output-schema flag was found.

Cursor has unconstrained JSON modes:

```text
--output-format <format>
    Output format (only works with --print):
    text | json | stream-json

--stream-partial-output
    Stream partial output as individual text deltas
    (only works with --print and stream-json format)
```

- `text`: plain final response.
- `json`: a single JSON result object on stdout; cacli expects final text in its `result` property.
- `stream-json`: JSONL events. Assistant messages use Claude-like content blocks:

```json
{
  "type": "assistant",
  "message": {
    "content": [
      {"type": "text", "text": "assistant text"}
    ]
  }
}
```

### Sandbox and permissions

Cursor now has an explicit native sandbox toggle:

```text
--sandbox <mode>
    Explicitly enable or disable sandbox mode (overrides config)
    choices: enabled, disabled
```

Other relevant controls:

```text
--mode plan   read-only/planning; analyze and propose, no edits
--mode ask    read-only Q&A
--plan        shorthand for --mode=plan
-f, --force   Force allow commands unless explicitly denied
--yolo        Alias for --force
--trust       Trust current workspace in print/headless mode
--workspace <path>
--worktree [name]
```

`plan`/`ask` are behavioral read-only modes. `--sandbox enabled` is the direct sandbox control. `--force` changes approvals and should not be treated as sandbox disabling.

### What cacli does today

Normal command:

```text
agent --print --output-format stream-json --force --model ... ...
```

`parse_output()` scans backward for the last `type: "assistant"` event and extracts the first content block’s `text`.

PR-description command uses:

```text
agent --print --output-format json --force --model ... ...
```

and extracts the top-level `result`.

Current gaps:

- `sandbox` is ignored despite native `--sandbox enabled|disabled`.
- `output_schema` is ignored because Cursor has no native schema flag.
- Commands always add `--force`, independently of the requested sandbox state.

### Recommendation

| Unified value | Recommended Cursor mapping |
|---|---|
| `sandbox=True` | Add `--sandbox enabled`. |
| `sandbox=False` | Add `--sandbox disabled`. |
| `sandbox=None` | Omit `--sandbox`, preserving Cursor’s config/default. |

Keep approval policy separate. In particular, decide independently whether cacli should continue always passing `--force`; do not infer it from `sandbox=False`.

Schema-required flow steps should be rejected for Cursor or explicitly use non-native prompt/post-validation.

---

## Summary

| Provider | Native schema flag | JSON mode | Native sandbox controls | cacli sandbox mapping today | Recommended `True / False / None` |
|---|---|---|---|---|---|
| Claude | `--json-schema <inline JSON>`; validated value in final result event/object’s `structured_output` | `--output-format json` single object; `stream-json` JSONL | Settings-based `sandbox.enabled`; macOS `/usr/bin/sandbox-exec`; permission flags are separate | Ignores all values | Settings `enabled=true` / `enabled=false` / omit override |
| Codex | `codex exec --output-schema <FILE>`; JSON text is final agent message or output-last-message file | `--json` JSONL; final `item.completed.item.type=agent_message`, text in `item.text` | `--sandbox read-only\|workspace-write\|danger-full-access` | `True→workspace-write+network`; `False→danger-full-access`; `None→True` | `workspace-write` / `danger-full-access` / omit |
| Devin | None | None on stdout; `--print` is plain text; `--export` is trajectory data | `--sandbox`; macOS Seatbelt, Linux bwrap+seccomp; omission disables | `True→--sandbox`; `False→omit`; `None→True` | add / omit / omit |
| Cursor | None | `--output-format json` single result object; `stream-json` JSONL assistant events | `--sandbox enabled\|disabled`; plan/ask are behavioral read-only modes | Ignores all values | enabled / disabled / omit |

The two immediate provider-layer correctness gaps are:

1. Wire native schemas for Claude and Codex, with different transport and extraction rules.
2. Wire Claude and Cursor sandbox controls, and change `None` for Codex/Devin to mean “do not override the provider default” if that is the flow engine’s declared tri-state contract.