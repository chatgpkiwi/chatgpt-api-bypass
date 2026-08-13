# Codex API token-optimization prompts — round 2

Read README.md to familiarize with the project.  

Implement only the phase that you were asked to do.  

## Phase 6 — Eliminate unusable approval prompt bloat

```text
Work in /home/lucio/chatgpkiwi/chatgpt-api-bypass on the existing codex-api.py
project after the persistent App Server migration is complete.

Goal: make every proxy-owned Codex thread use approval policy `never`, removing
the large model-visible escalation/approved-prefix prompt that is currently
injected even though this text-only proxy cannot service approval requests. Do
only this phase; do not disable skills, change prompt wrappers, add compaction,
or redesign MCP isolation yet.

Background verified with Codex CLI 0.144.6 and gpt-5.6-luna:
- The current App Server thread starts with `approval_policy: on-request`.
- Its first short turn reports about 4,096 logical input tokens.
- The persisted rollout shows that most of the excess is a host-generated
  permissions block containing escalation instructions and the user's complete
  approved-command-prefix list.
- Passing `approvalPolicy: "never"` to `thread/start` reduces the same short
  first turn to about 1,461 input tokens while preserving the read-only sandbox.
- AppServerClient rejects server-initiated requests and the lean catalog exposes
  no tools, so an interactive approval policy is both unusable and misleading.

Requirements:
1. Inspect codex-api.py, config.yaml, the lean profile, tests, README.md,
   TOKEN-BENCHMARK.md, the installed Codex version, and freshly generated App
   Server schemas before editing. Preserve unrelated changes.
2. Use the installed protocol's supported approval-policy field. Explicitly set
   `approvalPolicy: "never"` for both `thread/start` and `thread/resume`, so a
   proxy restart cannot restore an older persisted thread with `on-request`.
   If setting it on `turn/start` is also required by this installed protocol to
   guarantee the invariant, do so and explain why; avoid redundant settings if
   thread-level configuration is sufficient.
3. Keep the configured sandbox exactly as-is (`read-only` by default, or the
   caller/operator-selected existing value). Do not use danger-full-access or
   broaden filesystem/network access to shorten the prompt.
4. Keep normal interactive Codex sessions unchanged. Prefer explicit App Server
   request parameters owned by this proxy over modifying the user's global
   `~/.codex/config.toml`. If the dedicated lean profile is updated as defense in
   depth, make the smallest layered change and document every external file.
5. Preserve new-thread, resumed-thread, Chat Completions, `store: false`, state
   persistence, concurrency, health, and buffered streaming behavior.
6. Add focused fake-App-Server tests proving that new and resumed threads send
   the effective `never` policy and retain the configured sandbox. Add a test
   showing no unrelated request parameter changed.
7. Run syntax checks and the complete automated test suite.
8. Run a real local smoke test with the lean profile. Inspect a sanitized
   `debug prompt-input` capture or the new rollout locally and demonstrate that
   the long escalation instructions and approved-prefix list are absent. Never
   print the full approved-prefix list into the report or commit rollout data.
9. Measure at least three fresh first turns with the same short prompt, recording
   input, cached input, fresh input, output, reasoning output, and latency. Cache
   state cannot be cleared, so report individual trials and do not overstate
   cold-cache timing. Append a clearly labeled Phase 6 section to
   TOKEN-BENCHMARK.md; retain all earlier measurements.
10. Update README.md to explain why `approvalPolicy: never` is correct for this
    tool-free proxy and distinct from the read-only sandbox.

Implement and verify the change. At the end, report changed files, protocol
fields used, tests and smoke checks, before/after token counts, and any host
permission/environment text that still remains. Stop before Phase 7.
```

## Phase 7 — Disable all discovered skills

```text
Work in /home/lucio/chatgpkiwi/chatgpt-api-bypass after Phase 6 has set the App
Server approval policy to `never`.

Goal: prevent all Codex skills from being advertised to the model in this
text-only proxy, including skills installed now and skills added after a Codex
upgrade. Do only this phase; do not simplify request wrappers, add compaction,
or redesign MCP isolation yet.

Background verified with the current installation:
- `features.plugins = false` and model-catalog
  `include_skills_usage_instructions = false` do not suppress the App Server's
  host-generated `<skills_instructions>` catalog.
- Five system skills are currently advertised even though this proxy exposes no
  usable tools and cannot follow their workflows.
- Passing per-skill `enabled: false` entries through
  `thread/start.config.skills.config` removed the skills block and reduced a
  short first turn from about 1,461 to 833 input tokens.
- App Server provides `skills/list`; use the installed schema rather than
  hard-coding today's five skill paths if a robust dynamic solution is
  supported.

Requirements:
1. Inspect the completed Phase 6 code and tests, the lean profile/catalog,
   current skill roots, current official Codex documentation, and a freshly
   generated App Server schema. Preserve unrelated work.
2. Discover all skills that App Server would make available for the configured
   working directory. Prefer the stable `skills/list` protocol after the
   initialize/initialized handshake. Verify the exact request and response
   shapes for the installed CLI.
3. Build a thread-local configuration override that disables every discovered
   skill and apply it to both `thread/start` and `thread/resume`. Do not disable
   only a hard-coded list that silently becomes stale after upgrades. If the
   protocol cannot accept these overrides on resume, implement the smallest
   verified alternative and document it.
4. Decide and document fail-closed behavior. A discovery or configuration error
   must not silently start a model turn with the full skills prompt. Return a
   clear backend-unavailable/API error or use another verified no-skills path.
5. Avoid one discovery round trip per HTTP request when safe. Cache the disabled
   configuration for the one configured working directory and invalidate or
   refresh it when App Server emits the installed version's skills-change
   notification. A simple startup discovery is acceptable only if the schema
   and lifecycle guarantee skills cannot change unnoticed while the process is
   running.
6. Do not edit, delete, move, or globally disable the user's skill files. Normal
   Codex sessions must retain them. Keep all suppression process-local or
   thread-local to this proxy.
7. Preserve `approvalPolicy: never`, the read-only sandbox, empty model tool
   catalog, model/reasoning settings, new/resumed thread behavior, Chat
   Completions, `store: false`, concurrency, and shutdown behavior.
8. Extend the deterministic fake App Server to cover skill discovery, disabling
   configuration on new and resumed threads, discovery failure, malformed
   results, zero discovered skills, a future newly discovered skill, and cache
   invalidation if implemented. Tests must consume no real model tokens.
9. Run syntax checks and the full automated tests.
10. Run a real local prompt-input/rollout smoke check demonstrating that
    `<skills_instructions>` and all skill names/descriptions are absent from the
    model-visible prompt. Keep raw rollout contents out of the repository.
11. Repeat the short first-turn benchmark and append a Phase 7 section to
    TOKEN-BENCHMARK.md, retaining earlier results and clearly separating logical
    input from cached/fresh input.
12. Update README.md with the discovery/disable lifecycle and upgrade behavior.

Implement and verify the change. At the end, list protocol calls and config
fields used, failure behavior, tests, prompt evidence, token change, and every
modified file. Stop before Phase 8.
```

## Phase 8 — Remove the redundant wrapper for plain input

```text
Work in /home/lucio/chatgpkiwi/chatgpt-api-bypass after Phases 6 and 7 have
removed approval and skill prompt bloat.

Goal: add a semantics-preserving fast path for the common Responses API case
where `input` is a plain string and top-level `instructions` is absent. Send that
string directly as the App Server turn text instead of wrapping it in repeated
instructions and `<user>` tags. Do only this phase; do not remove role wrappers
where roles are needed, add compaction, or redesign MCP isolation.

Background:
- `codex-api-lean-instructions.md` already tells the model to follow the supplied
  input and return only the requested assistant answer.
- `prompt_from_responses_request` repeats that instruction and adds `<user>`
  tags on every turn.
- In the optimized App Server test, the repeated wrapper added about 29 tokens
  on turn 1, 58 logical tokens by turn 2, and 87 by turn 3 because earlier
  wrapped messages remain in conversation history.
- The exact three-turn sample used 2,656 input tokens with the wrapper and 2,482
  with raw string turns while preserving the expected conversational memory.

Requirements:
1. Inspect prompt construction, request validation, App Server turn input,
   response tests, the lean base-instructions file, and completed Phase 7
   behavior before editing. Preserve unrelated changes.
2. For `/v1/responses` only, when `input` is a string and `instructions` is
   absent or null, return/send the exact input string as turn text. Do not trim,
   normalize, escape, add labels, or otherwise mutate caller text.
3. Preserve the existing unambiguous wrapper when non-empty top-level
   `instructions` is supplied or when `input` is an array of role-bearing
   messages. System/developer/assistant roles must not be flattened into an
   unsafe or ambiguous raw string.
4. Preserve Chat Completions prompt rendering, which remains stateless and must
   carry its full role-labeled history on each call.
5. Do not make the base instructions empty merely to save roughly another 28
   tokens. Retain the short behavioral/tool-free guardrail unless concrete
   tests show a smaller wording with equivalent behavior across every supported
   request form.
6. Preserve all validation and error behavior for missing, null, empty, invalid,
   and oversized inputs. Confirm the intended handling of an empty string
   explicitly rather than changing it accidentally.
7. Add focused unit tests for: plain string/no instructions fast path; null
   instructions; non-empty developer instructions; message arrays with each
   supported role; unsupported roles/items; exact whitespace and delimiter-like
   text; empty strings; and unchanged Chat Completions rendering.
8. Run syntax checks and the full automated suite.
9. Run a real local three-turn Responses conversation equivalent to:
   `My name is Lucio.`, `What is my name?`, and
   `reply with my name in all caps.` Confirm memory and expected final behavior.
   Record per-turn logical, cached, fresh, output, and reasoning tokens.
10. Append a Phase 8 section to TOKEN-BENCHMARK.md comparing the wrapped and raw
    paths fairly. Update README.md to document when the fast path applies.

Implement and verify the change. At the end, summarize the exact fast-path
condition, behavior retained for structured inputs, tests, smoke output at a
safe summary level, and measured savings. Stop before Phase 9.
```

## Phase 9 — Add safe long-conversation compaction

```text
Work in /home/lucio/chatgpkiwi/chatgpt-api-bypass after Phases 6–8 have reduced
the fixed first-turn prompt.

Goal: add compaction support for long stateful Responses conversations so
history does not grow without bound, while preserving `previous_response_id`
semantics, per-turn locking, accurate usage, and OpenAI-compatible behavior
where it can be implemented faithfully. Do only this phase; do not redesign MCP
isolation yet.

Use the current official OpenAI Responses compaction documentation, current
Codex App Server documentation, and freshly generated schemas from the installed
CLI. The App Server currently exposes `thread/compact/start`, while the proxy
currently rejects `context_management`. Do not assume that the native App
Server method has the exact wire semantics of OpenAI's stateless
`/responses/compact` endpoint.

Requirements:
1. Inspect codex-api.py, response-state versioning, App Server event routing,
   token accounting, tests, README.md, TOKEN-BENCHMARK.md, current official
   compaction guidance, and generated protocol schemas before designing the
   change. Preserve unrelated work.
2. First write down the semantic mapping you will implement:
   - stateful continuation uses the stored Codex thread selected by
     `previous_response_id`;
   - native compaction is triggered through the installed App Server protocol;
   - the compact operation must finish before the user's next turn begins;
   - compact and user turn form one serialized operation under the existing
     per-thread lock;
   - compaction cost/usage must not disappear from accounting.
3. Support the currently documented Responses `context_management` compaction
   form to the extent it can be mapped faithfully. Validate the array/object,
   type, threshold, and error cases. A request without `previous_response_id`
   should not perform pointless pre-turn compaction.
4. Track enough per-response state to decide whether the active rendered
   context has crossed the requested threshold. Do not confuse cumulative token
   usage across calls with the latest turn's logical input/context size. Version
   persisted state compatibly and retain loading of older records.
5. Implement an AppServerClient compaction operation using the exact installed
   notification sequence. Await its context-compaction lifecycle and terminal
   turn status, handle errors/timeouts/child death, and prevent its events from
   being mistaken for or discarded by the following user turn.
6. Return honest usage. If one HTTP response causes both a compaction model pass
   and the requested user turn, aggregate the relevant input/output/reasoning/
   cache counters when the protocol exposes them. Document any counter the
   installed App Server does not expose; never report only the cheaper half of
   the work as if it were complete usage.
7. Investigate `/v1/responses/compact` separately. Implement it only if this
   proxy can honor the current official stateless request and canonical-output
   semantics. Do not label a proprietary `{previous_response_id: ...}` endpoint
   as OpenAI-compatible. If exact support is not feasible with the proxy's
   text-wrapper/thread architecture, document that limitation and expose no
   misleading endpoint.
8. Preserve previous-response chaining, `store: false`, new/resumed threads,
   Chat Completions, same-thread serialization, cross-thread concurrency,
   buffered streaming, response IDs, and restart behavior.
9. Add deterministic tests covering: below-threshold no-op; threshold-triggered
   compact then turn; compact event ordering; compact timeout/failure; malformed
   notifications; combined usage; two simultaneous requests for one thread;
   independent-thread concurrency; legacy state; `store: false`; invalid
   context-management input; and proxy restart before later compaction. Consume
   no real model tokens in automated tests.
10. Run syntax checks and the full suite. Then perform a bounded real smoke test
    on a disposable thread, using a deliberately low but valid threshold only
    for the test. Confirm a compaction event occurs and the conversation retains
    an important fact afterward. Do not alter the default production threshold
    merely to make the smoke test easy.
11. Add documentation explaining that compaction has its own token cost, can
    reset a reusable cache prefix, and should be reserved for genuinely long
    conversations. Document automatic versus caller-selected thresholds and
    rollback/error behavior. Append a Phase 9 functional check to
    TOKEN-BENCHMARK.md without pretending a tiny three-turn chat benefits.

Implement and verify compaction, not a stub. At the end, summarize the semantic
mapping, state-format change, event flow, accounting, endpoint compatibility,
tests, smoke evidence, and remaining limitations. Stop before Phase 10.
```

## Phase 10 — Prevent inherited MCP prompt leakage

```text
Work in /home/lucio/chatgpkiwi/chatgpt-api-bypass after Phases 6–9 are complete.

Goal: guarantee that no MCP server configured for the user's normal Codex
sessions can leak tool schemas, resource helpers, startup prose, or prompt
tokens into this text-only proxy—now or after another MCP server is added. Do
not weaken authentication or normal Codex configuration.

Background:
- The current lean profile explicitly disables only the existing
  `openaiDeveloperDocs` server.
- The App Server process still loads the user's base Codex configuration before
  applying profile-derived overrides.
- A future `[mcp_servers.<name>]` entry can therefore become active in the proxy
  unless it is explicitly suppressed.
- The model currently has no MCP schemas, so this phase is primarily a
  future-proof isolation guarantee rather than a promise of a large immediate
  token reduction.

Requirements:
1. Inspect AppServerClient command construction, named-profile translation,
   current global/profile configuration, MCP startup behavior, tests, current
   official Codex configuration/App Server documentation, and freshly generated
   schemas. Preserve unrelated work and never print or commit credentials.
2. Choose the smallest supported architecture that guarantees an empty MCP set.
   Investigate in this order:
   - a verified process- or thread-local configuration override that replaces
     or disables every inherited MCP server;
   - enumerating effective configured MCP server names and generating explicit
     `enabled = false` overrides before any thread starts;
   - a dedicated minimal `CODEX_HOME` only if supported overrides cannot provide
     the guarantee.
   Do not assume that `mcp_servers = {}` replaces rather than merges config;
   prove the effective behavior with the installed CLI.
3. If enumerating servers, derive names from parsed/effective configuration and
   handle arbitrary valid TOML keys safely. Disable every server, not only
   `openaiDeveloperDocs`, and refresh the set when the App Server process is
   restarted. Fail closed if configuration cannot be inspected or suppression
   cannot be verified.
4. If a dedicated `CODEX_HOME` is necessary, keep it outside the repository,
   create it with restrictive permissions, and document lifecycle/backup. Never
   copy authentication secrets into version control or logs. Reuse supported
   authentication/credential mechanisms rather than inventing token extraction.
   Preserve required model catalog, profile, session-resume, and state behavior.
5. Ensure MCP servers are disabled before `thread/start`; starting them and
   merely hiding tools afterward is insufficient. No MCP child process or
   network connection should be created for proxy turns when avoidable.
6. Preserve all preceding optimizations: approval never, read-only sandbox,
   skills disabled, lean catalog/base instructions, raw-string fast path, and
   compaction. Normal interactive Codex sessions must retain their MCP servers.
7. Add deterministic tests with zero, one, and several inherited MCP servers;
   unusual valid server names; disabled and required servers; malformed config;
   profile overrides; a newly added future server; startup/restart; and fail-
   closed behavior. Assert the exact App Server command/config or effective
   thread settings rather than relying on model behavior.
8. Run syntax checks and the full automated suite.
9. Perform a local smoke test using a temporary harmless fake MCP entry or
   isolated test configuration. Demonstrate through effective config,
   `mcpServerStatus/list`, prompt input, sanitized rollout data, or equivalent
   local evidence that the proxy exposes zero MCP servers/tools/resources while
   the user's normal Codex configuration remains unchanged.
10. Update README.md with the isolation mechanism, operational checks, upgrade
    procedure, failure mode, and rollback. Append a Phase 10 verification note
    to TOKEN-BENCHMARK.md; report token counts honestly even if the current
    configuration yields no measurable reduction.

Implement and verify the isolation guarantee. At the end, report the selected
architecture and why, effective MCP evidence, tests, smoke checks, files changed
inside and outside the project, token impact, security considerations, and any
remaining unavoidable Codex host context.
```
