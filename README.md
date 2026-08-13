# chatgpt-api-bypass

The program codex-api.py is a local API that exposes Codex CLI through an OpenAI compliant HTTPS listener.

Why?

The ChatGPT API costs money to use, whereas Codex limits are included in ChatGPT plus subscription.  
Therefore, we use a custom API that points to Codex instead of ChatGPT API. 

So is this just to save money?  
Yes. No. Yes. 

## How does it work

1. Install Codex CLI and authenticate.
2. Install ChatGPT-API-Bypass. Edit config.yaml. You can run codex-api manually or install the optional systemd service.
4. Your apps that point to ChatGPT API, point them to your codex-api instead. 

## Is it just like using ChatGPT API?

No. The Codex CLI injects a bit of tool-calling instruction bloat to your prompts. So it uses more tokens than a pure prompt. We stripped out as much of it as we could but at the end of the day, your prompts still get some extra text from Codex CLI, ~1k extra tokens per call.

## No encryption?

codex-api opens only HTTP non-encrypted ports. If you want to run it as a server for remote clients, we recomment you install a serious firewall in front of codex-api, like [proxyble](www.proxyble.com). 

## Optional systemd service

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

We are still working on improving codex-api.py. The next tasks involve stripping out some Codex CLI built-in prompts for tooling, so that the context is as pure as possible. 

We still need to implement some OpenAI API endpoints such as compact, for conversation compaction.

## Lean Codex profile

The proxy always uses the fixed `codex-api-lean` profile. At every startup it
checks `~/.codex/codex-api-lean.config.toml` (or the equivalent under
`CODEX_HOME`) and atomically recreates it when it is missing, unreadable, or
points at the wrong instructions file. Set `profile_instructions` in
`config.yaml` to the project-owned instruction file; relative paths are
resolved from that YAML file. No manual Codex profile setup is required.

The generated profile points to that instructions file and disables
text-proxy-unneeded Codex features and the configured `openaiDeveloperDocs`
MCP server. The proxy keeps its configured `read-only` sandbox; this profile
only minimizes prompt context and available tools.

The profile replaces built-in instructions, disables `AGENTS.md` injection,
web search, apps, plugins, remote-plugin discovery, multi-agent collaboration,
goals, shell/exec tools, shell snapshots, image generation, browser/computer
use, tool suggestions, and the existing Docs MCP server. Codex CLI 0.144.6
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

The persisted response state now records the Codex thread ID and the cumulative
Codex usage snapshot for each stored response.  When a request uses
`previous_response_id`, the proxy subtracts that predecessor snapshot, so the
returned Responses usage describes the resumed turn rather than the whole
thread.  The state file is written atomically and is upgraded to version 2 on
the next stored response; existing version-1 records containing only a
`thread_id` remain usable.

For a continuation from one of those legacy records, or when Codex supplies
missing, malformed, or regressing counters, an exact delta cannot be derived.
The proxy logs a warning and returns sanitized non-negative Codex totals as a
documented compatibility fallback.  A newly stored response then supplies a
snapshot for its successor.  `store: false` responses are never persisted, but
may still use their stored predecessor to calculate their own delta.

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
- `turn/start` sends the same text wrappers used by the former `codex exec`
  backend. `item/completed` supplies final `agentMessage` text and
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
configured instruction file, `model_catalog_json`, feature disables, and the
disabled Docs MCP server; it neither edits the user's base
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
