# Design

A walk-through of every feature.

## Systemd

On Linux systems using systemd, `codex-api-systemd` can install the proxy as a
per-user service. It writes `codex-api.service` under
`$XDG_CONFIG_HOME/systemd/user` (normally `~/.config/systemd/user`), using
absolute paths to this project's `codex-api.py` and `config.yaml`. This lets it
use the same user-local Codex installation, configuration, and credentials as
your shell. Installation enables it for the user manager, starts it immediately,
and restarts it after failures.

```shell
chmod +x codex-api-systemd
./codex-api-systemd install
systemctl --user status codex-api
```

The helper also supports `start`, `stop`, `restart`, and `uninstall`:

```shell
systemctl --user stop codex-api
systemctl --user start codex-api
./codex-api-systemd uninstall
```

Do not run the helper with `sudo`. To keep the proxy running after logout and
start it after a reboot before you log in, enable lingering once:

```shell
sudo loginctl enable-linger "$USER"
```

If you previously installed the old system-wide unit, stop and remove it first
to avoid two proxies competing for the same port:

```shell
sudo systemctl disable --now codex-api
sudo rm -f /etc/systemd/system/codex-api.service
sudo systemctl daemon-reload
```

It uses `.venv/bin/python` when the project has one, otherwise `python3` from
your `PATH`. Set `CODEX_API_PYTHON=/absolute/path/to/python` during `install`
to select a particular interpreter. Re-run `install` after changing the
interpreter, shell `PATH`, or project location so the generated unit picks up
the new values.

The proxy intentionally implements a focused text-only subset of the OpenAI
surface. See the sections below for the current stateful Responses and native
compaction behavior, rather than assuming every OpenAI endpoint is available.

## Lean Codex profile

The proxy always uses the fixed `codex-api-lean` profile. At every startup it
checks `~/.codex/codex-api-lean.config.toml` (or the equivalent under
`CODEX_HOME`) and atomically recreates it when it is missing, unreadable, or
points at the wrong instructions file. Set `profile_instructions` in
`config.yaml` to the project-owned instruction file; relative paths are
resolved from that YAML file. No manual Codex profile setup is required.

The generated profile points to that instructions file and disables
text-proxy-unneeded Codex features. The proxy keeps its configured `read-only`
sandbox; this profile only minimizes prompt context and available tools.

The profile replaces built-in instructions, disables `AGENTS.md` injection,
web search, apps, plugins, remote-plugin discovery, multi-agent collaboration,
goals, shell/exec tools, shell snapshots, image generation, browser/computer
use, and tool suggestions. Codex CLI 0.144.6
does not accept the newer `tools.view_image`/`tools.web_search` settings; its
supported top-level `web_search = "disabled"` setting is used instead.

`codex debug prompt-input` still renders host-provided permission, skill, and
environment metadata in this installation. Those sources have no supported
profile-level suppression in CLI 0.144.6. Model-catalog-provided tool metadata,
including `apply_patch`, is removed by Phase 3's project-owned
`codex-api-model-catalog.json`. The catalog contains a verbatim copy of the
active `gpt-5.6-luna` record from the installed CLI, except that
`apply_patch_tool_type` and `multi_agent_version` are null, `tool_mode` is
`direct`, and experimental tools are empty. The profile alone points to this
catalog, so normal Codex sessions retain their usual catalog.

Run `python3 update_model_catalog.py` after upgrading Codex. It reads `codex
debug models`, refuses an unreviewed catalog schema or source tool change, and
writes the catalog atomically. Then run `python3 update_model_catalog.py
--check`, `codex --profile codex-api-lean debug prompt-input "Say hello."`, and
the smoke command below before using the upgraded CLI with this proxy:

```shell
codex exec --profile codex-api-lean --model gpt-5.6-luna \
  --config 'model_reasoning_effort="low"' --ephemeral --sandbox read-only \
  --json 'Say hello.'
```

The `debug prompt-input` output is expected to contain no tool-schema names.
The JSONL smoke output should contain only `thread.started`, `turn.started`, an
assistant `item.completed`, and `turn.completed`; any tool-call event is a
validation failure.

## Responses usage on continued conversations

The persisted response state now records the Codex thread ID, cumulative Codex
usage snapshot, and latest rendered-context input-token count for each stored response.  When a request uses
`previous_response_id`, the proxy subtracts that predecessor snapshot, so the
returned Responses usage describes the resumed turn rather than the whole
thread.  The state file is written atomically and is upgraded to version 3 on
the next stored response; existing version-1 and version-2 records remain
usable. Version-1 records containing only a
`thread_id` remain usable.

For a continuation from one of those legacy records, or when Codex supplies
missing, malformed, or regressing counters, an exact delta cannot be derived.
The proxy logs a warning and returns sanitized non-negative Codex totals as a
documented compatibility fallback.  A newly stored response then supplies a
snapshot for its successor.  `store: false` responses are never persisted, but
may still use their stored predecessor to calculate their own delta.

### Long Responses conversations and compaction

For a stateful `/v1/responses` continuation, callers may use the documented
server-side selector:

```json
{"context_management":[{"type":"compaction","compact_threshold":200000}]}
```

The proxy applies it only with a valid `previous_response_id`. Before the next
user turn, it compares the predecessor's saved latest rendered-context input
count with `compact_threshold`; it deliberately does not use lifetime cumulative
usage. At or above the threshold it calls Codex App Server's native
`thread/compact/start`, waits for its compaction item, usage update, and
successful terminal turn, then starts the user turn under the same per-thread
lock. This preserves ordering and prevents compaction events from leaking into
the following turn.

There is no production-wide automatic threshold: compaction is caller-selected
per request. New threads do not perform pointless pre-turn compaction. Older
state records lack the context-size field, so their first continuation safely
skips proxy-triggered compaction; the next stored response upgrades the record.
`store: false` responses are not saved and cannot become a later compaction
predecessor.

Compaction is a model pass with token cost. When App Server exposes both passes'
counters, returned Responses usage adds input, cached input, output, and
reasoning-output counters to the requested turn; optional cache-write counters
are included only when exposed for every pass. Compaction can reset a reusable
cache prefix, so reserve it for genuinely long conversations. A timeout,
malformed event, failure status, missing compaction item, or missing usage event
fails the HTTP request and does not start the user turn.

`POST /v1/responses/compact` is intentionally not exposed. That official
endpoint is stateless and returns a canonical opaque compacted input window,
whereas this proxy stores opaque Codex threads and only supports text wrappers.
The supported `context_management` mapping retains stateful continuation, but
cannot expose OpenAI's encrypted compaction item in its buffered text-only
response output.

## Persistent App Server backend

The proxy now starts one local `codex app-server --stdio` child as part of its
ASGI lifespan. It completes the required JSON-RPC `initialize` / `initialized`
handshake before serving requests and closes stdin on shutdown, waits up to five
seconds, then force-kills only if needed. `/health` reports `200` only when the
HTTP service and initialized child are both usable; a dead or uninitialized
child produces `503` with `backend: "unavailable"`.

The client uses the stable v2 protocol generated from Codex CLI 0.144.6:

- `thread/start` creates new Responses conversations and ephemeral stateless
  Chat Completions threads.
- `thread/resume` reloads a saved Codex thread after a proxy restart.
- `turn/start` sends the rendered text input. `item/completed` supplies final `agentMessage` text and
  `thread/tokenUsage/updated.tokenUsage.last` supplies per-turn usage.

Public `resp_...` IDs remain separate from Codex thread IDs. Stored Responses
records keep the App Server's cumulative `tokenUsage.total` snapshot for
backward-compatible state; returned usage comes directly from its incremental
`tokenUsage.last` counters. `store: false` does not create a public response
mapping and therefore cannot be resumed. Independent threads may run up to
`max_concurrent_requests` at once, while a lock serializes turns for a single
thread. Streaming remains buffered and preserves the previous SSE shapes.

### Lean profile with App Server

Codex CLI 0.144.6 rejects `--profile` on `app-server`. After reconciling the
fixed profile at startup, the proxy reads it and passes every setting as a
process-local supported `--config dotted.key=value` override. This includes the
configured instruction file, `model_catalog_json`, and feature disables; it
neither edits the user's base
`~/.codex/config.toml` nor changes normal interactive Codex sessions.

The App Server itself adds a larger fixed host prompt than `codex exec` in this
CLI release. The profile/catalog settings remain active, but the Phase 5 timing
benchmark below shows that its first-turn logical input count is not directly
comparable to the lean `exec` figure. This is a known current limitation, not a
change to caller-visible prompt wrappers or model settings.

### Approval policy and sandbox

Every proxy-owned App Server thread explicitly sends `approvalPolicy: "never"`
on both `thread/start` and `thread/resume`. The proxy exposes no model tools and
rejects App Server-initiated requests, so it cannot present or fulfill an
interactive approval request. Setting this policy prevents the host from
advertising unusable escalation instructions and approved-command prefixes to
the model, including when a persisted thread is reloaded after a proxy restart.

This is separate from the sandbox. The proxy still sends the configured
`sandbox` value unchanged (`read-only` by default; `workspace-write` only when
the operator explicitly selects it). `approvalPolicy: "never"` does not widen
filesystem or network access, alter the user's global Codex configuration, or
change normal interactive Codex sessions.

### Skill discovery and suppression

After the App Server `initialize` / `initialized` handshake, the proxy calls
`skills/list` with its configured working directory. It builds a thread-local
`config.skills.config` override with `enabled: false` for every returned skill
path and sends that override on both `thread/start` and `thread/resume`. This
removes skill instructions from proxy-owned model context without editing,
moving, or disabling any user or system skill globally; normal Codex sessions
retain their configured skills.

The discovered disable configuration is cached for this one working directory,
not hard-coded to the skills present when the proxy was released. If the App
Server sends `skills/changed`, the proxy invalidates that cache and rediscovers
the complete list before the next new or resumed thread. A failed, partial, or
malformed discovery is fail-closed: startup (or the pending thread setup after
an invalidation) returns an App Server/API error rather than starting a model
turn with the full skills catalog. Therefore, after a Codex upgrade, newly
installed skills are suppressed automatically when discovery succeeds; inspect
the startup error and regenerate the installed schema if it does not.

### MCP isolation

Before launching App Server, the proxy parses the reachable user, system, and
project Codex configuration layers plus its managed lean profile, collecting
only MCP server names (never credentials, headers, commands, URLs, or other
server values). It then passes one process-local TOML override such as
`mcp_servers={server_name={enabled=false}}`. This is deliberately not
`mcp_servers = {}`: Codex merges configuration tables, so an empty table leaves
inherited servers active. The inline-table override merges each named server
entry and forces its `enabled` leaf to `false` before App Server can create a
stdio child process or network connection. Quoted TOML names are emitted safely
inside that table.

After the initialize handshake, the proxy calls `mcpServerStatus/list` and
fails startup unless each returned configured-but-disabled entry has no server
metadata, tools, resources, or resource templates. CLI 0.144.6 includes
disabled configured names in this inventory, so a nonzero status-entry count is
normal; an initialized server or any exposed MCP content is not. Malformed or
paginated inventories, unreadable/malformed configuration, and any residual
MCP content make `/health` unavailable and prevent all proxy turns. The set is
reparsed whenever the App Server process starts, so restart the proxy after
adding or changing an MCP server or after a Codex upgrade.

This is process-local and does not edit the user's normal `config.toml`, MCP
credentials, or interactive Codex sessions. To operationally check it, restart
the proxy and confirm a healthy startup; if it fails, inspect the non-secret
MCP table structure and regenerate the App Server schema after upgrades. To
roll back, stop the proxy and return to the prior project version; no global MCP
configuration needs restoration. Do not bypass a failed MCP check by removing
the verification: fix or disable the offending normal-session configuration
explicitly instead.

### Responses plain-input fast path

For `/v1/responses`, a present string `input` with `instructions` omitted or
explicitly `null` is sent to App Server exactly as supplied. The lean base instructions
already define the text-only response contract, so this avoids adding a repeated
response directive and `<user>` wrapper to every saved turn. Whitespace and
delimiter-like caller text are preserved exactly, including an empty string.

Inputs with a non-empty top-level `instructions` value, an empty string
`instructions` value (for backward-compatible rendering), or an array of
role-bearing input messages retain the existing unambiguous role-labelled
wrapper. Chat Completions also retains its complete stateless role-labelled
history on every request.

### Operations and rollback

Start normally with `python3 codex-api.py`; Uvicorn drives the ASGI lifespan.
The optional `app_server_start_timeout_seconds` setting (or
`--app-server-start-timeout` / `CODEX_API_APP_SERVER_START_TIMEOUT`) bounds the
startup handshake. On an unavailable backend, inspect the bounded local stderr
lines and run:

```shell
codex app-server generate-json-schema --out /tmp/codex-app-server-schema
codex app-server --stdio
```

The first command regenerates the installed CLI's schema for diagnosis; the
second is only a transport check against the user's normal configuration. The
proxy constructs its exact profile-derived config list from the selected named
profile in `_profile_config_overrides`; this avoids relying on unsupported
`app-server --profile` syntax.
Do not enable App Server's network listener: this proxy uses only local stdio.

There is intentionally no runtime fallback to `codex exec`, so a backend death
cannot silently change conversation behavior. To roll back, stop the proxy and
restore the pre-Phase-5 `codex-api.py` from version control, then restart:

```shell
git restore --source=<commit-before-phase-5> -- codex-api.py config.yaml README.md
python3 codex-api.py
```

Use the commit that contained the previous `codex exec` implementation; the
persisted version-2 response state remains readable by that implementation.
