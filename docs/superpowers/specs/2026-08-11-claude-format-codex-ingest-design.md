# Claude-format transcripts in the Codex bucket

**Date:** 2026-08-11  
**Status:** Approved design  
**Scope:** Extend Codexmeter; preserve every existing parser path

## Context

Claude Code now runs GPT models through `claude-code-proxy`. Its archived sessions are routed to the Codex bucket by provider, but the stored JSONL and key layout remain Claude Code's. Codexmeter already detects these files and deliberately skips them so they remain unstamped and can be ingested when support lands (`backend/parse.py:475`, `backend/ingest.py:101`).

The current session proves the useful model identity survives in the payload: its assistant records use `message.model = "gpt-5.6-sol"`. The parser can therefore attribute and price the effective GPT model without inferring from a Claude Code dispatch alias.

## Decision

Add a dedicated Claude-format adapter to Codexmeter and route detected files into it. Adapt Claudit's mature parser semantics onto Codexmeter's shared parse, pricing, and persistence contracts. Extend the current format dispatcher and ingest pipeline; do not replace or fork the existing Kimi or Codex paths.

This is preferable to importing Claudit at runtime, which would couple two independent deployments, and to placing a second large parser inside `backend/parse.py`, which would mix format detection with format-specific state.

## Goals

1. Ingest both new and previously skipped Claude-format transcripts from the Codex bucket.
2. Preserve all existing Kimi and Codex records and metric semantics; the only parser-contract addition is `prompt_count: 0`.
3. Provide all available metrics: usage and cost, context turns, substantive prompt count, tool calls and errors, edit churn, visible response size, reply latency, and rate-limit hits.
4. Attribute records from `message.model` and price GPT models through Codexmeter's existing rates, including long-context billing.
5. Support main and subagent transcripts, plain JSONL and JSONL.XZ.
6. Keep archive-key project/session identity stable while displaying a consistent transcript `cwd` when available.

## Non-goals

- Runtime imports from `/root/claudit`.
- Changing the archive format or moving objects between buckets.
- Replacing existing Kimi or native Codex parsers.
- Guessing a provider model from aliases when `message.model` is absent or unknown.
- Adding new dashboard panels unrelated to making the parsed metrics available.

## Architecture

### 1. Format adapter

Create `backend/parse_claude.py`. Port the behavior of `/root/claudit/backend/parse.py`, but use Codexmeter's output vocabulary and shared primitives from `backend/parse_common.py`:

- request-ID max merge for streamed assistant records;
- iteration flattening and peak-per-iteration context input;
- substantive user-prompt detection, replay suppression, and interrupt handling;
- assistant visible-text measurement;
- tool-use extraction, result/error reconciliation, and Edit/Write churn;
- reply-latency anchoring;
- rate-limit extraction;
- context-turn construction.

Claude-specific mutable state remains in the adapter: request merge maps, seen user UUIDs, prompt boundaries, tool IDs, and candidate `cwd` values. Shared billing/tool/turn builders remain format-neutral.

`parse_claude.parse(file_key, blob)` returns Codexmeter's standard payload plus:

- `prompt_count`: substantive user prompts;
- `project_display_name`: a consistent absolute `cwd`, otherwise absent.

### 2. Detection and dispatch

Keep `backend.parse.parse_file` as the sole format detector. Preserve the current detection precedence:

1. native Codex markers;
2. Kimi Code markers;
3. legacy Kimi markers;
4. Claude markers (`sessionId` or unkeyed file-history types).

Replace only the current Claude `UnsupportedTranscriptError` result with `parse_claude.parse(file_key, blob)`. A line containing both a native marker and `sessionId` continues to select the native parser.

`UnsupportedTranscriptError` may remain for future recognized-but-unsupported formats, but Claude transcripts no longer raise it.

### 3. Ingest normalization

Replace the scan's split between native work and counted foreign objects with a normalized candidate carrying:

- R2 object metadata;
- `project_id`;
- `session_id`;
- `is_main`;
- initial project display label;
- layout/format hint only where needed for key interpretation.

Native layout remains:

`/sessions/<project-id>/<session-id>/wire.jsonl`

Claude layout becomes first-class:

`/<project-slug>/<session-id>/<session-id>.jsonl[.xz]` for the main transcript, with nested data/subagent JSONL files marked `is_main = false`.

The project slug is the stable `project_id`; the second key component is the session ID. The main-file test strips `.jsonl` or `.jsonl.xz` and compares the basename with the session ID.

Planning, ETag/parser-version invalidation, fetching, persistence, canonical recomputation, rollups, and orphan deletion remain shared. Previously skipped objects have no `files` rows, so the first post-release ingest naturally treats them as new work without a separate backfill.

### 4. Project labels

The adapter gathers non-empty absolute `cwd` values. If all usable values in a transcript agree, it returns that path as `project_display_name`; otherwise ingest retains the project slug. Persistence may update the display label without changing `project_id`.

### 5. Metrics and pricing

The adapter trusts the payload's `message.model`. Known values such as `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna` resolve exactly through `backend/pricing.py`. Unknown values retain the existing estimated fallback rather than being remapped heuristically.

Usage maps as follows:

| Claude-format usage field | Codexmeter field |
|---|---|
| `input_tokens` | fresh input |
| `cache_creation_input_tokens` | cache creation |
| `cache_read_input_tokens` | cache read |
| `output_tokens` | output |

When `iterations` is present, billing fields are summed across iterations while context input uses the largest individual iteration total, matching Claudit's established behavior. Long-context pricing is enabled when the request's fresh + creation + read input exceeds Codexmeter's existing threshold.

Codexmeter does not introduce Anthropic TTL pricing. Cache creation remains one flat Codexmeter category because these are GPT-billed sessions in the Codex bucket.

### 6. Prompt count and persistence

Add `files.prompt_count INT NOT NULL DEFAULT 0`. Every parser payload carries `prompt_count`; existing formats return zero until they have an equivalent substantive-prompt definition. Persist it alongside `turn_count`, and expose it wherever session metadata already exposes turn count. No standalone dashboard panel is required.

### 7. Browser parser

The Inspector's `src/parser.js` remains an independent offline parser. Extend its detector rather than replacing its native Codex/Kimi logic, adapting the relevant Claude-format behavior from Claudit's browser parser. Mirror model-rate behavior and preserve the backend/JavaScript parity tests.

## Error handling

- Malformed JSON lines are skipped, matching both existing codebases.
- Deterministic parse exceptions are recorded as ingest failures and never create or update a `files` row.
- A valid Claude-format file with no usage records may persist as a truthful empty session; it is no longer confused with an unsupported format.
- Unknown model pricing remains explicitly estimated through the current resolution path.
- Missing or conflicting `cwd` metadata falls back to the stable project slug.
- Existing skip accounting remains available for future recognized-but-unsupported formats, but successfully parsed Claude files count as inserted/reparsed rather than skipped.

## Change inventory

| Area | Change |
|---|---|
| `backend/parse_claude.py` | New dedicated format adapter based on Claudit semantics and Codexmeter contracts. |
| `backend/parse.py` | Dispatch detected Claude files to the adapter; preserve native precedence. |
| `backend/parse_common.py` | Add only genuinely format-neutral helpers/output support needed by the adapter, including default prompt count. |
| `backend/ingest.py` | Normalize both key layouts into one work plan and persist display metadata/prompt count. |
| `backend/schema.sql` | Add idempotent `files.prompt_count`. |
| `backend/api_sessions.py` | Return prompt count with existing session metadata. |
| `backend/pricing.py` | No new rates expected; use existing GPT rates and long-context meter. |
| `src/parser.js` | Add Claude-format dispatch/parse support without replacing native parsing. |
| `fixtures/parser/` | Add focused GPT-backed Claude-format fixtures; retain existing detection fixture. |
| `fixtures/r2_mini/` or test mirrors | Cover main/subagent and compressed foreign-layout objects. |
| `tests/` | Parser, ingest, API, browser parity, and regression coverage. |
| `AGENTS.md` / `README.md` | Replace “skipped foreign transcript” behavior with supported-layout documentation. |

## Verification

### Parser tests

- GPT model ID is preserved and priced exactly.
- Streamed records sharing a request ID max-merge once.
- Iterations sum billing but use peak context.
- Synthetic responses do not erase prior context turns.
- Replayed prompts do not distort latency anchors.
- Instrumentation and interrupt records do not count as substantive prompts.
- Tool calls, matched success/error results, unmatched results, and failed-edit churn behave as in Claudit.
- Reply latency, text characters, rate-limit hits, and context deltas are retained.
- Native Codex/Kimi fixtures still select their original parsers and preserve every existing record and metric, with only the additive `prompt_count: 0` contract field.

### Ingest tests

- Root-layout main and nested subagent files are inserted, not skipped.
- `.jsonl` and `.jsonl.xz` derive identical identities.
- Project slug, session ID, `is_main`, and consistent `cwd` label are correct.
- A formerly skipped file with no row is ingested automatically.
- The second unchanged run is idempotent.
- Parser failures remain unstamped and retryable.
- Removing an object deletes its rows and rebuilds derived state.

### Gates

Run the focused parser and ingest suites first, then the repository's full pytest, lint, type, JavaScript mirror, and secrecy checks. Bump the deployed `PARSER_VERSION` so any already-stamped parser outputs reparse; the previously skipped Claude-layout files need no backfill.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Port drifts from Claudit | Port behavior through named fixtures and document the source semantics; do not import the other checkout at runtime. |
| Native parser regression | Preserve detection precedence and maintain exact native fixture assertions. |
| Incorrect model cost | Trust `message.model`, use existing exact resolver, and test all known GPT IDs plus unknown fallback. |
| Duplicate main/subagent records | Preserve UUID canonicalization and request-ID within-file merging; test cross-file overlap. |
| Nondeterministic project labels | Use `cwd` only when internally consistent; otherwise retain stable slug. |
| Large historical first ingest | Reuse existing bounded fetch chunks and persistence pipeline; no separate all-at-once backfill. |

## Approval

The user granted standing approval on 2026-08-11 for the most correct design rather than the easiest implementation. This design selects the dedicated adapter and shared ingest normalization on that basis.
