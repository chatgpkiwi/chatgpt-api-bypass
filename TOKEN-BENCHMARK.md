# First-turn token benchmark

## Conclusion

The dedicated `codex-api-lean` profile and project-owned text-only model
catalog materially reduce fixed first-turn context. Across three fresh,
ephemeral runs, median logical input fell from **11,067** tokens to **1,429**
tokens: a reduction of **9,638 tokens (87.09%)**. The reported fresh
(non-cached) input has the same median reduction in this sample. Median
end-to-end CLI elapsed time fell from 6,311 ms to 3,264 ms (48.28%), but this
is only an approximate timing comparison because the service-side cache state
could not be cleared.

## Environment

| Setting | Value |
| --- | --- |
| Date | 2026-08-12 (America/Los_Angeles) |
| Codex CLI | `codex-cli 0.144.6` |
| Model | `gpt-5.6-luna` |
| Reasoning effort | `low` (`model_reasoning_effort="low"`) |
| Sandbox | `read-only` |
| Working directory | `/home/lucio/Documents` |
| Lean profile | `codex-api-lean` at `~/.codex/codex-api-lean.config.toml` |
| Lean catalog | `codex-api-model-catalog.json` (validated with `python3 update_model_catalog.py --check`) |

`config.yaml` selects `profile_instructions: codex-api-lean-instructions.md`,
`model: gpt-5.6-luna`,
`thinking_effort: low`, the working directory above, and `sandbox: read-only`.
The profile points only at the project catalog and the short project-owned
instructions file. `codex --profile codex-api-lean debug prompt-input 'Say
hello.'` rendered a substantially smaller local prompt-input JSON capture than
the normal configuration (14,415 vs. 19,964 bytes). This is a diagnostic
indicator only; the JSON byte counts are not token counts.

## Methodology

Every trial used the identical prompt, `Say hello.`, in a new ephemeral Codex
session. Each JSONL capture contained its own `thread.started` event before
`turn.completed`; the thread IDs in the table demonstrate that no trial resumed
or reused a thread. `--skip-git-repo-check` is included because the configured
working directory is not a trusted Git repository and the proxy supplies that
same flag. Wall-clock latency covers the local process invocation through
completion of JSONL output.

Control used the normal configuration, model metadata, and instructions. Lean
used the dedicated profile, including its custom model catalog. Apart from that
profile selection, model, reasoning effort, sandbox, directory, prompt, and
ephemeral execution were held constant.

```sh
# Control (repeat three times)
codex exec --skip-git-repo-check --sandbox read-only --color never \
  --model gpt-5.6-luna --config 'model_reasoning_effort="low"' \
  --ephemeral --cd /home/lucio/Documents --json 'Say hello.'

# Lean (repeat three times)
codex exec --profile codex-api-lean --skip-git-repo-check --sandbox read-only \
  --color never --model gpt-5.6-luna --config 'model_reasoning_effort="low"' \
  --ephemeral --cd /home/lucio/Documents --json 'Say hello.'
```

The counters below are from the JSONL `turn.completed.usage` object.
`fresh_input_tokens` is calculated as `input_tokens - cached_input_tokens`.
Codex did not emit `cache_write_input_tokens` in any trial, so it is recorded
as `—`. All `reasoning_output_tokens` values were zero.

## Results

| Configuration | Trial | `thread.started` ID | Input | Cached input | Fresh input | Cache write | Output | Reasoning output | Latency (ms) |
| --- | ---: | --- | ---: | ---: | ---: | --- | ---: | ---: | ---: |
| Control | 1 | `019ff85c-e411-7780-8c7c-91921b10635e` | 11,275 | 0 | 11,275 | — | 11 | 0 | 9,233 |
| Control | 2 | `019ff85d-0286-7140-b563-b40b917fd9cb` | 11,067 | 8,960 | 2,107 | — | 11 | 0 | 6,087 |
| Control | 3 | `019ff85d-1a52-7663-bc57-4b50bb74ebfb` | 11,067 | 0 | 11,067 | — | 11 | 0 | 6,311 |
| Lean | 1 | `019ff85d-32be-7f31-a68f-3ca64f404f53` | 1,429 | 0 | 1,429 | — | 6 | 0 | 5,268 |
| Lean | 2 | `019ff85d-475f-7813-b662-0d7d47467379` | 1,429 | 0 | 1,429 | — | 6 | 0 | 1,959 |
| Lean | 3 | `019ff85d-a12b-7001-bf87-8a4ce7ee42a9` | 1,429 | 0 | 1,429 | — | 6 | 0 | 3,264 |

| Median | Control | Lean | Absolute change | Percentage reduction |
| --- | ---: | ---: | ---: | ---: |
| Total logical input | 11,067 | 1,429 | -9,638 | 87.09% |
| Cached input | 0 | 0 | 0 | not meaningful |
| Fresh input | 11,067 | 1,429 | -9,638 | 87.09% |
| Output | 11 | 6 | -5 | 45.45% |
| Reasoning output | 0 | 0 | 0 | not meaningful |
| Wall-clock latency | 6,311 ms | 3,264 ms | -3,047 ms | 48.28% |

## Cache and latency interpretation

This is not a cold-cache benchmark: cache state cannot be manually cleared.
Control trial 2 reported 8,960 cached input tokens while all other benchmark
trials reported zero cached input. The total logical-input reduction is still
the direct measure of removed model-visible context. The fresh-input median is
also strongly favorable here, but cache variability and shared local/service
conditions mean the latency result should be treated as approximate rather than
as a controlled cold-cache latency claim.

## Local proxy cross-check

The proxy was started on a separate temporary local listener using the normal
configuration with a temporary state file, then a new-conversation request was
made (no `previous_response_id`):

```sh
python3 codex-api.py --port 18000 --state-file /tmp/codex-api-benchmark-state.json
curl -H 'content-type: application/json' \
  -d '{"model":"gpt-5.6-luna","input":"Say hello."}' \
  http://127.0.0.1:18000/v1/responses
```

The response completed in 2,105 ms and reported 1,458 input tokens, 0 cached
input tokens, 6 output tokens, 0 reasoning output tokens, and 1,464 total
tokens. The matching lean direct CLI trial reported 1,429 input, 0 cached, 6
output, and 0 reasoning tokens. The 29 additional input tokens are explainable
by the proxy's Responses request wrapper around the user input; output and
cache counters agree. This was a first-turn request with no cumulative-usage
subtraction involved.

## Limitations

- These are only three trials per configuration, executed serially on one
  machine and service account.
- Cache state, local startup work, network conditions, and service scheduling
  were uncontrolled; latency is therefore indicative, not a cold-cache SLA.
- The control and lean answers had different output-token counts despite the
  same short prompt, so output and total-token comparisons are secondary to
  the input-context result.
- Raw JSONL captures are intentionally retained only in `/tmp`, outside the
  repository. They contain no authentication material and are not needed to
  reproduce the commands above.

## Phase 5 — persistent App Server latency check

This is a separate measurement from the Phase 4 `codex exec` comparison above.
It measures a fresh, ephemeral App Server thread for the same `Say hello.`
prompt, while retaining one initialized local stdio App Server process across
the three turns. Process startup and the initialize handshake are excluded from
each latency measurement because that is precisely the work removed from normal
requests. The model, low reasoning effort, read-only sandbox, working directory,
lean profile-derived settings, and custom catalog remained unchanged.

The installed CLI was still `codex-cli 0.144.6`. The protocol was generated
locally first with:

```sh
codex app-server generate-json-schema --out /tmp/codex-app-server-schema
```

Then the proxy's App Server client was started once and timed around each
`thread/start` + `turn/start` completion. Every trial returned a distinct thread
ID, so no trial resumed a prior conversation.

| Trial | Thread ID | Input | Cached input | Fresh input | Output | Reasoning output | Turn latency (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `019ff86d-07da-7dd3-bfbe-b7c168ea0e2c` | 4,064 | 0 | 4,064 | 6 | 0 | 2,317 |
| 2 | `019ff86d-10e8-7c93-a6b6-5ab3fbe012ea` | 4,064 | 0 | 4,064 | 6 | 0 | 1,862 |
| 3 | `019ff86d-182d-7db1-b769-094a47a55373` | 4,064 | 0 | 4,064 | 6 | 0 | 1,755 |
| Median | — | 4,064 | 0 | 4,064 | 6 | 0 | **1,862** |

Against the Phase 4 lean `codex exec` median of 3,264 ms, the persistent
App Server turn median is **1,402 ms faster (42.95% lower)**. Cache state and
service conditions were not controlled, so this is indicative rather than a
cold-cache SLA. The App Server reports 4,064 logical input tokens versus the
Phase 4 direct-CLI median of 1,429: its own fixed host prompt is larger in this
CLI version. This does not negate the latency win from avoiding a process per
request, but it means token-context figures across the two transports should
not be treated as like-for-like prompt-bloat measurements.

## Phase 6 — non-interactive approval policy

This measurement repeats the Phase 5 App Server method after the proxy began
sending `approvalPolicy: "never"` on every `thread/start` and `thread/resume`.
The sandbox remained `read-only`; no filesystem or network permission was
broadened. The same initialized local App Server process served three distinct,
fresh ephemeral threads with the same `Say hello.` prompt. Latency starts before
`thread/start` and ends after `turn/completed`; it excludes process startup and
the initialize handshake.

| Trial | Thread ID | Input | Cached input | Fresh input | Output | Reasoning output | Turn latency (ms) |
| ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 1 | `019ff9e7-53b7-77d2-b1e3-fb186b453c93` | 1,429 | 0 | 1,429 | 6 | 0 | 1,853 |
| 2 | `019ff9e7-5af5-7170-897f-b6fec41b7b9e` | 1,429 | 0 | 1,429 | 6 | 0 | 5,980 |
| 3 | `019ff9e7-7251-7fd2-9075-26e0fd199934` | 1,429 | 0 | 1,429 | 6 | 0 | 2,219 |
| Median | — | **1,429** | **0** | **1,429** | **6** | **0** | **2,219** |

Relative to the Phase 5 App Server median, logical and fresh input both fell
from 4,064 to 1,429 tokens: **2,635 tokens (64.84%)** removed. The observed
median latency increased by 357 ms, but cache state cannot be cleared and
service conditions were uncontrolled, so this is not a cold-cache latency
comparison.

A sanitized local `codex --profile codex-api-lean --config
'approval_policy="never"' debug prompt-input 'Say hello.'` capture was valid
JSON and contained neither escalation-instruction markers nor approved-command
prefix-list markers. The raw capture remains only in `/tmp` and is not checked
into the repository. It still contains `<skills_instructions>`; skill removal
is intentionally outside Phase 6.
