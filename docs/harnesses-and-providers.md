# Harnesses and providers

cacli used to call the CLI it runs a "provider". That conflated two things:

- **Harness**: the agent CLI that runs (Claude Code, Codex, Pi, OpenCode,
  Cursor, Devin). It owns the command line, output parsing, and log format.
- **Provider**: where inference happens and which account pays for it
  (`claude-sub`, `claude-api`, `openai-sub`, `openai-api`, `bedrock`,
  `workers-ai`, `cursor`, `devin`). It owns the endpoint, the credential, and
  how that credential travels on the wire.

A run is `(harness, provider, model)`. The model is the provider's own model
ID, unqualified (`claude-opus-5-5`, `gpt-6-sol`, `@cf/zai-org/glm-5.3`).

Subscription vs. API billing is a provider choice (`claude-sub` vs.
`claude-api`), not a separate flag. Bedrock is a provider, usable from Claude
Code and Codex.

Gemini CLI is gone (Google deprecated it). GLM, Kimi, and MiniMax via their
own companies' endpoints are not supported; GLM-5.3 runs on Workers AI.

## Supported pairs

Each harness declares the providers it can drive. A pair that is not listed
is an error that names the supported providers for that harness.

| Harness | Providers (first is the default) |
|---|---|
| claude | claude-sub, claude-api, bedrock |
| codex | openai-sub, openai-api, bedrock, workers-ai |
| pi | workers-ai, claude-api, openai-api |
| opencode | workers-ai, claude-api, openai-api |
| cursor | cursor |
| devin | devin |

Subscription providers pair only with their own vendor's harness.

Pairs marked unverified in code (`verified=False`) are wired from
documentation and have not been run against the live service. Currently: codex
with bedrock and codex with workers-ai.

## Launch spec

`build_launch(harness, prompt, *, provider=None, model=None, params=None, ...)`
returns a `LaunchSpec`:

- `command`: the shell command. Non-secret env the harness needs, and the
  unsetting of env vars that would change which credential the harness uses,
  are baked into the command with `env -u NAME ... NAME=value <harness>`, so
  the command is correct by itself regardless of the ambient environment.
- `env`: the non-secret env vars the command sets (for callers that build a
  sandbox environment).
- `unset_env`: env vars that must not reach the harness. For example,
  `claude-sub` unsets `ANTHROPIC_API_KEY`, which Claude Code would otherwise
  prefer over `CLAUDE_CODE_OAUTH_TOKEN`.
- `credentials`: the secrets the caller must supply, and how each travels on
  the wire (see below). cacli never holds secret values.

`params` are non-secret provider parameters, such as the Bedrock region or the
Cloudflare account ID. Each has a conventional env var (`AWS_REGION`,
`CLOUDFLARE_ACCOUNT_ID`). The library takes them explicitly and raises when a
required one is missing. The `cacli` CLI reads them from the environment.

## Credentials and wire bindings

Each `Credential` names where the harness reads it (an env var, or a file for
OAuth logins) and, when known, a `WireBinding`: the hosts, header or query
parameter, value format, methods, and path prefix it is sent with. Params are
substituted, so the Workers AI binding carries the exact account path prefix.

Callers that broker secrets through a proxy (ccremote's secproxy / Sail
policies) compile these bindings into their network policy. A credential with
no binding cannot be brokered. The reason is stated, e.g. an OAuth login file
whose refresh tokens the harness rotates itself.

| Provider | Credential | Wire binding |
|---|---|---|
| claude-sub | `CLAUDE_CODE_OAUTH_TOKEN` | `Authorization: Bearer` on api.anthropic.com |
| claude-api | `ANTHROPIC_API_KEY` | `x-api-key` on api.anthropic.com |
| openai-sub | Codex `auth.json` (file) | none: OAuth refresh |
| openai-api | `OPENAI_API_KEY` | `Authorization: Bearer` on api.openai.com |
| bedrock | `AWS_BEARER_TOKEN_BEDROCK` | `Authorization: Bearer` on bedrock-runtime.{region}.amazonaws.com |
| workers-ai | `CLOUDFLARE_API_KEY` | `Authorization: Bearer` on api.cloudflare.com, `GET`/`POST`, prefix `/client/v4/accounts/{account_id}/ai/` |
| cursor | `CURSOR_API_KEY` | none: wire format not documented |
| devin | Devin config files | none: login files |
