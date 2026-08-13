# Codex API optimization prompts

Run these prompts in order, one prompt per clean Codex chat. Before starting each
phase, preserve unrelated work and inspect the files left by the previous phase.
Do not combine phases: each prompt explicitly defines its own stopping point.

## Phase 1 — Fix cumulative token accounting

```text
Work in /home/lucio/Documents on the existing codex-api.py project.

Goal: fix token accounting for continued Responses API conversations. Do only
this phase; do not create a lean Codex profile, custom model catalog, benchmark,
or App Server integration yet.

Background:
- codex-api.py invokes `codex exec --json` and translates the
  `turn.completed.usage` object into OpenAI-compatible usage fields.
- For a newly started Codex thread, those counters describe the first turn.
- For `codex exec resume <thread-id>`, Codex CLI currently reports cumulative
  thread totals, not the incremental usage of only the resumed turn.
- For example, first and second cumulative cached-input totals of 9,984 and
  19,968 mean the second turn used 9,984 cached tokens, not 19,968.
- Chat Completions requests are stateless and should continue reporting the
  counters directly. This correction applies to `/v1/responses` continuation
  through `previous_response_id`.

Requirements:
1. Inspect codex-api.py, config.yaml, and the current Codex JSONL event format
   before editing. Preserve unrelated user changes.
2. Extend the persisted response state so every stored public response ID can
   carry both its Codex thread ID and the cumulative Codex usage totals observed
   at that point. Load the existing version-1 state format safely; do not break
   users who already have response records containing only `thread_id`.
3. On a continued `/v1/responses` request, subtract the previous response's
   cumulative counters from the newly returned cumulative counters before
   converting them to the OpenAI Responses usage shape. Handle at least:
   `input_tokens`, `cached_input_tokens`, `output_tokens`, and
   `reasoning_output_tokens`. Preserve any relevant cache-write counter if the
   installed Codex version emits one and the OpenAI-compatible response has an
   appropriate detail field.
4. Never emit negative usage. If cumulative counters regress, are missing, or
   cannot be compared, fail safely with a clear logged warning and a documented
   fallback; do not silently return nonsensical numbers.
5. The first turn, stateless Chat Completions calls, and unrelated response
   fields must retain their current behavior.
6. `store: false` must retain its existing semantics. A stored predecessor may
   be used to calculate the current turn's delta even when the new response is
   not stored.
7. Keep state writes atomic and do not corrupt the state file if persistence
   fails.
8. Add focused automated tests for first-turn usage, two or more resumed turns,
   cached and reasoning token deltas, legacy state loading, malformed/regressing
   totals, and `store: false`. Use the project's existing test style if one
   exists; otherwise use Python's standard library unittest so no new testing
   dependency is required.
9. Run syntax checks and the focused tests. If feasible, run a local end-to-end
   two-turn Responses request and show that the second response reports the
   per-turn delta rather than the cumulative total.

Do not merely describe the fix: implement and verify it. At the end, summarize
changed files, compatibility behavior, tests run, and any remaining limitation.
```

## Phase 2 — Add a dedicated lean/text-only Codex profile

```text
Work in /home/lucio/Documents on the existing codex-api.py project after the
cumulative token-accounting phase has been completed.

Goal: add and activate a dedicated lean/text-only Codex configuration profile
for this proxy, using supported Codex configuration controls to remove as much
agent prompt and tool context as possible. Do only this phase; do not add a
custom model catalog, benchmark final token counts, or migrate to App Server.

Use the current OpenAI Codex documentation/manual and the installed CLI's
`codex debug prompt-input` and `codex features list` commands as the source of
truth. Configuration details can change, so verify every key against this
installed Codex version before committing it.

Requirements:
1. Inspect codex-api.py, config.yaml, the installed Codex version, the effective
   Codex configuration, and existing user/profile files. Preserve unrelated
   settings and never overwrite the user's main ~/.codex/config.toml wholesale.
2. Create a named profile dedicated to this proxy in the location and format
   expected by the installed Codex CLI. Use a clear name such as
   `codex-api-lean`. Point config.yaml at that profile while retaining the
   existing command-line/environment override precedence.
3. Create a small, project-owned base-instructions file for the profile. It
   should make the model act as a text response engine: follow the supplied
   conversation/input, return only the requested assistant answer, and do not
   attempt tool use. Keep it short and avoid duplicating the wrapper instructions
   already constructed by codex-api.py.
4. Through supported configuration, suppress all context that is unnecessary
   for a text-only HTTP proxy, including where supported:
   - built-in model instructions, replaced by the small instructions file;
   - permissions/sandbox instruction prose;
   - apps, plugins, recommended-plugin/tool-suggestion guidance;
   - collaboration and multi-agent tools/instructions;
   - skills instructions;
   - environment-context injection;
   - AGENTS.md/project-document injection (`project_doc_max_bytes = 0`);
   - shell/exec, image viewing/generation, browser/computer use, goals,
     web search, planning, and request-user-input tools;
   - MCP tools and MCP resource helper tools. In particular, ensure the existing
     `openaiDeveloperDocs` MCP server is not exposed in this proxy profile.
5. Do not weaken the proxy's process sandbox setting. This is prompt/tool
   minimization, not permission expansion.
6. Do not silently change the intended model or reasoning effort as part of this
   phase. If config.yaml currently contains an invalid value for the installed
   CLI, identify it and make the smallest necessary correction, explaining it.
7. Do not pretend that `apply_patch` has a normal feature toggle if it does not.
   Record any remaining model-metadata-driven tool or context for Phase 3.
8. Verify the effective result with `codex debug prompt-input` using the exact
   profile the proxy will use. Compare the model-visible prompt before and after
   without sending private contents to external services. Also run one harmless
   `codex exec --ephemeral --sandbox read-only --json "Say hello."` through the
   profile to confirm the profile loads and returns text.
9. Add concise project documentation explaining where the profile lives, how
   config.yaml selects it, and how `--profile` overrides it. Do not add measured
   final token results yet; that belongs to Phase 4.

Implement and verify the profile. At the end, list every enabled/disabled prompt
or tool source, show the diagnostic result at a useful summary level, identify
anything that remains, and list all files created or modified both inside and
outside the project.
```

## Phase 3 — Use a custom model catalog to remove remaining tool metadata

```text
Work in /home/lucio/Documents on the existing codex-api.py project after the
lean profile phase has been completed.

Goal: use Codex's supported `model_catalog_json` mechanism to remove the final
model-metadata-driven tool schemas from the proxy, especially `apply_patch`,
without forking or rebuilding Codex. Do only this phase; do not run the formal
Phase 4 benchmark or migrate to App Server.

Use the current OpenAI Codex documentation/manual, the installed Codex CLI, and
its current raw model catalog as sources of truth. Do not hand-invent a stale or
minimal model record: Codex model-catalog schemas evolve.

Requirements:
1. Inspect the active model/profile from config.yaml and the installed CLI's raw
   model catalog (`codex debug models` or the current documented equivalent).
   Preserve all metadata required for the selected model to work correctly.
2. Create a project-owned custom model-catalog JSON file in a reproducible way.
   Prefer a small documented generation/update script if safely maintaining the
   catalog requires copying and patching the installed catalog. Do not modify
   ~/.codex/models_cache.json in place.
3. For the proxy's selected model, preserve its slug, capabilities, context
   window, reasoning settings, WebSocket preference, compaction metadata, and
   other required fields, changing only the fields needed for text-only use.
   The intended effective changes are:
   - `apply_patch_tool_type: null`;
   - direct/text-only tool mode rather than Code Mode;
   - multi-agent metadata disabled;
   - no experimental supported tools unless genuinely required.
   Verify the exact enum spellings and schema accepted by this installed CLI.
4. Point only the dedicated proxy profile at this custom catalog. Do not change
   normal interactive Codex sessions or replace the user's global catalog.
5. Retain all Phase 2 prompt/tool suppression. Ensure no MCP, shell, browser,
   image, collaboration, planning, or other tool schema reappears.
6. Add a repeatable validation/update procedure. It must fail clearly if a
   future Codex catalog schema changes instead of silently generating a broken
   catalog. Document that upgrading Codex requires regenerating and validating
   this file.
7. Verify the profile and catalog with the installed CLI. Use
   `codex debug prompt-input`, generated schemas/debug output, trace output, or
   another reliable local mechanism to demonstrate that the model-visible tool
   set is empty. Do not rely only on the model choosing not to call tools.
8. Run a harmless read-only, ephemeral JSON execution for "Say hello." and
   confirm the selected OpenAI model still answers normally and no tool-call
   events occur.
9. Add focused tests for any generation/validation script without introducing
   unnecessary dependencies.

Implement and verify this supported no-fork solution. At the end, summarize the
catalog fields changed, evidence that the effective tool list is empty, commands
run, maintenance procedure, and all files modified. If the installed CLI cannot
reliably eliminate all tools through `model_catalog_json`, stop and document the
specific evidence rather than proceeding to a source fork.
```

## Phase 4 — Measure the new first-turn token count

```text
Work in /home/lucio/Documents on the existing codex-api.py project after the
lean profile and custom model-catalog phases have been completed.

Goal: measure and document the actual first-turn token and latency improvement.
This is an evidence/benchmark phase. Do not change proxy behavior, tune prompts
during measurement, or migrate to App Server.

Requirements:
1. Inspect the completed profile/catalog configuration and confirm the proxy
   selects them. Record the installed Codex version, selected model, reasoning
   effort, sandbox, working directory, and exact commands used.
2. Design a fair control-versus-lean comparison using fresh first-turn,
   ephemeral sessions and the exact same prompt: `Say hello.` The control must
   use the normal model metadata and instructions; the lean case must use the
   dedicated proxy profile and custom catalog. Do not resume a thread in this
   comparison.
3. Run at least three trials per configuration. Capture from Codex JSONL:
   `input_tokens`, `cached_input_tokens`, any `cache_write_input_tokens`,
   `output_tokens`, `reasoning_output_tokens`, and wall-clock latency. Calculate
   fresh/non-cached input as `input_tokens - cached_input_tokens` while clearly
   retaining the total logical input count.
4. Because cache state cannot be manually cleared, do not claim the timing is a
   cold-cache benchmark unless it truly is. Report individual trials and median
   values, and explain whether cache hits make latency comparisons approximate.
5. Also run one new-conversation request through `/v1/responses` on the local
   proxy and verify its reported usage agrees with the lean direct CLI run within
   explainable wrapper differences. Do not use `previous_response_id` for this
   first-turn check.
6. Validate that the formal measurement is not accidentally cumulative. Include
   the `thread.started` evidence showing each trial is a new thread.
7. Create or update a project report named TOKEN-BENCHMARK.md. Include:
   - environment and methodology;
   - exact reproducible commands;
   - a table of every trial;
   - medians and absolute/percentage token reductions;
   - cached versus uncached interpretation;
   - the proxy endpoint cross-check;
   - limitations and a clear conclusion on whether the fixed context bloat was
     materially reduced.
8. Leave temporary raw captures outside the repository unless they are small,
   sanitized, and necessary for reproducibility. Never commit authentication
   material or full private rollout contents.

Perform the benchmark and write the report. At the end, give the before/after
median total input, cached input, fresh input, latency, and percentage reduction.
Do not implement Phase 5 in this session.
```

## Phase 5 — Migrate to a persistent Codex App Server

```text
Work in /home/lucio/Documents on the existing codex-api.py project after Phases
1–4 have been completed. Treat running this prompt as confirmation that the
measured latency is still undesirable and that the migration should proceed.

Goal: replace one-new-`codex exec`-process-per-request with one persistent local
`codex app-server` process while preserving the proxy's OpenAI-compatible HTTP
surface, lean profile/custom catalog, sandboxing, conversation semantics, token
usage, and clean shutdown behavior.

Use the current OpenAI Codex App Server documentation and generate the protocol
schema from the installed CLI before designing the integration. Do not assume
method names or notification shapes from memory. Prefer the stable local stdio
JSONL transport; do not expose App Server's experimental unauthenticated network
listener.

Requirements:
1. Inspect codex-api.py, config.yaml, response-state format, tests,
   TOKEN-BENCHMARK.md, the installed Codex version, and the generated App Server
   JSON schema. Preserve unrelated changes.
2. Start one long-lived `codex app-server` subprocess using the dedicated lean
   profile and custom model catalog. Integrate its startup and shutdown with the
   ASGI lifespan. Complete the required `initialize`/`initialized` handshake
   before accepting model requests.
3. Implement a robust asynchronous JSON-RPC client for the stdio JSONL
   transport:
   - monotonically unique request IDs and pending futures;
   - a single stdout reader that dispatches responses and notifications;
   - concurrent requests for independent threads;
   - per-thread serialization for turns on the same conversation;
   - bounded timeouts and useful errors;
   - continuous stderr draining into sanitized logs;
   - detection of malformed messages and unexpected process exit;
   - graceful termination with forced kill only after a bounded shutdown wait.
4. Conversation mapping:
   - a new `/v1/responses` request creates an App Server thread with
     `thread/start`, then sends input through `turn/start`;
   - a request with `previous_response_id` continues the mapped Codex thread;
   - when state survives a proxy restart, use `thread/resume` before the next
     turn;
   - keep OpenAI-style public response IDs separate from Codex thread IDs;
   - retain `store: false` behavior without making an unstored response
     resumable;
   - retain per-thread locking and allow configured concurrency across different
     threads.
5. Preserve Chat Completions' current stateless semantics by creating an
   ephemeral App Server thread per request. Do not accidentally give separate
   Chat Completions calls shared history.
6. Translate the current request prompt wrappers into App Server turn text
   without changing their meaning. Preserve model, reasoning effort, working
   directory, read-only/workspace-write sandbox choice, and timeout settings.
7. Collect the final assistant text and per-turn token usage from the exact App
   Server notifications defined by this installed version. Return incremental
   OpenAI-compatible usage, not cumulative thread totals. Keep Phase 1's
   compatibility logic only where it remains needed.
8. Preserve `/health`, `/v1/models`, `/v1/chat/completions`, `/v1/responses`,
   authorization, JSON error shapes, non-streaming responses, and existing
   buffered SSE response shapes. True live delta streaming may be added only if
   it can be done cleanly without destabilizing compatibility; otherwise leave
   it for a later phase and document that streams remain buffered.
9. Make `/health` distinguish an HTTP listener that is alive from an App Server
   backend that is initialized and usable. Return a clear unavailable status if
   the child process has died.
10. Add configuration only where needed (for example App Server startup or
    restart policy), with command-line values overriding environment/YAML as in
    the existing program. Avoid breaking existing config.yaml users.
11. Add comprehensive automated tests using a deterministic fake App Server
    subprocess or transport. Cover handshake, new thread, resume after restart,
    two concurrent independent threads, same-thread serialization, text and
    usage extraction, timeout, malformed JSON, child death, `store: false`, both
    HTTP endpoints, and graceful shutdown. Tests must not consume real model
    tokens.
12. Run focused tests, syntax/static checks available in the project, and real
    local smoke tests against the installed App Server for:
    - stateless Chat Completions;
    - a two-turn Responses conversation that remembers the first turn;
    - a proxy restart followed by continuation using persisted state;
    - concurrent independent requests;
    - token usage correctness;
    - process cleanup with no orphaned App Server.
13. Re-run a small latency comparison against the Phase 4 baseline and append a
    clearly separated App Server result to TOKEN-BENCHMARK.md. Do not rewrite the
    earlier measurements.
14. Update project documentation with architecture, lifecycle, troubleshooting,
    and the exact rollback path to the last `codex exec` implementation. If a
    temporary compatibility switch between backends is useful and small, add it;
    otherwise keep the migration focused.

Implement and verify the migration, not just a prototype. At the end, summarize
the architecture, protocol methods actually used, compatibility decisions,
tests/smoke checks, latency result, rollback procedure, and remaining limitations.
```
