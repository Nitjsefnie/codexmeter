# Claude-format Codex Ingest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse Claude Code JSONL sessions archived into the Codex bucket, preserving every existing parser while providing full Codexmeter metrics and first-class ingest for Claude key layouts.

**Architecture:** Add a dedicated `backend/parse_claude.py` adapter derived from Claudit semantics, dispatched by the existing detector and projected through Codexmeter's shared pricing/output contract. Normalize native and Claude archive keys into one ingest candidate type so planning, persistence, canonicalization, rollups, and invalidation stay shared; extend the browser Inspector additively.

**Tech Stack:** Python 3.13+, FastAPI, orjson, PostgreSQL/psycopg3, pytest, browser JavaScript tested through the existing Node-based pytest harness, Cloudflare R2/file mirror.

## Global Constraints

- Extend the existing Kimi and Codex parsers; never replace or bypass them.
- Do not import `/root/claudit` at runtime and do not add a package dependency between the repositories.
- Preserve existing native records and metric semantics; the only parser-contract addition is `prompt_count: 0` for formats without equivalent accounting.
- Trust `message.model`; do not infer GPT identities from Claude Code aliases.
- Use existing Codexmeter rates and `LONG_CONTEXT_THRESHOLD = 272_000`; long context means `ctx_input > 272_000`.
- Cache creation is Codexmeter's flat category; do not introduce Anthropic TTL buckets.
- The archive key slug is the stable `project_id`; a single consistent absolute transcript `cwd` may become `display_name`.
- Malformed JSON lines are skipped; deterministic parser failures remain unstamped and retryable.
- Fixtures, not production R2, drive tests.
- Work on `master` without a worktree. Commit each task locally; push the completed batch once.
- Implementation commits use `Co-Authored-By: GPT-5.6 Sol <noreply@openai.com>`.
- Source design: `docs/superpowers/specs/2026-08-11-claude-format-codex-ingest-design.md`.

## File Structure

- Create `backend/parse_claude.py`: all Claude-format line interpretation and per-file state.
- Modify `backend/parse_common.py`: one explicit usage-record constructor and additive prompt-count output.
- Modify `backend/parse.py`: detection/dispatch only; existing Kimi implementation remains in place.
- Modify `backend/ingest.py`: normalized transcript candidates, foreign-layout planning, display metadata, prompt persistence.
- Modify `backend/schema.sql`: idempotent `files.prompt_count` migration.
- Modify `backend/api_sessions.py`: expose prompt count with session context metadata.
- Modify `src/parser.js`: add Claude-format Inspector parsing without replacing native browser paths.
- Add focused fixtures under `fixtures/parser/`; reuse file-mode mirrors constructed by ingest tests.
- Add `tests/test_parse_claude.py`; modify parser, ingest, API, and JS tests only where their contracts grow.
- Modify `AGENTS.md`, `README.md`, and deployment parser-version configuration after behavior is verified.

---

### Task 1: Add the shared additive parser contract

**Files:**
- Modify: `backend/parse_common.py:36-59,145-197,242-257`
- Modify: `backend/schema.sql:10-27`
- Modify: `backend/ingest.py:882-942`
- Modify: `tests/test_parse.py`
- Modify: `tests/test_ingest.py`

**Interfaces:**
- Produces: `_make_usage_record(file_key, line_num, ts, uuid, model, toks, *, text_chars=0, reply_latency_s=None, ctx_input=None, reasoning=0, long_context=False) -> dict`.
- Produces: every parse result has integer `prompt_count`; existing formats return `0`.
- Produces: `files.prompt_count INT NOT NULL DEFAULT 0` persisted on insert and update.

- [ ] **Step 1: Write failing native-contract tests**

Add to `tests/test_parse.py`:

```python
@pytest.mark.parametrize("name", [
    "single_turn.jsonl",
    "kimi_code.jsonl",
])
def test_existing_formats_add_zero_prompt_count(name):
    out = parse.parse_file("sessions/p/s/wire.jsonl", _read(name))
    assert out["prompt_count"] == 0
```

Add to the existing ingest row assertions in `tests/test_ingest.py`:

```python
with db.viz_conn() as c:
    rows = c.execute(
        "SELECT prompt_count FROM files ORDER BY file_key"
    ).fetchall()
assert rows
assert {row[0] for row in rows} == {0}
```

- [ ] **Step 2: Run the tests and verify the contract is missing**

Run:

```bash
python3 -m pytest tests/test_parse.py::test_existing_formats_add_zero_prompt_count tests/test_ingest.py -q
```

Expected: parser test fails with `KeyError: 'prompt_count'` and ingest/schema use of `prompt_count` is not yet available.

- [ ] **Step 3: Factor the format-neutral record constructor**

In `backend/parse_common.py`, add this constructor and make `_append_usage_record` call it:

```python
def _make_usage_record(
    file_key: str,
    line_num: int,
    ts: datetime | None,
    uuid: str | None,
    model: str,
    toks: tuple[int, int, int, int],
    *,
    text_chars: int = 0,
    reply_latency_s: float | None = None,
    ctx_input: int | None = None,
    reasoning: int = 0,
    long_context: bool = False,
) -> dict:
    fresh, create, read, output = toks
    cost = pricing.compute_cost(
        model,
        fresh=fresh,
        create=create,
        read=read,
        output=output,
        long_context=long_context,
    )
    return {
        "file_key": file_key,
        "line_num": line_num,
        "uuid": uuid,
        "ts": ts,
        "model": model,
        "fresh_tokens": fresh,
        "cache_creation_tokens": create,
        "cache_read_tokens": read,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "cost_usd": round(cost, 6),
        "text_chars": text_chars,
        "reply_latency_s": reply_latency_s,
        "ctx_input": fresh + create + read if ctx_input is None else ctx_input,
    }
```

Replace `_append_usage_record`'s inline dict with `_make_usage_record(...)`, passing `st.text_chars_since_turn` and the consumed latency. Existing parser output must remain equal field-for-field.

Add `prompt_count: int = 0` to `_ParseState` and include it in `_finish_parse`:

```python
return {
    "records": st.records,
    "ctx_turns": ctx_turns,
    "turn_count": len(ctx_turns),
    "prompt_count": st.prompt_count,
    "rate_limit_hits": st.rate_limit_hits,
    "tool_uses": st.tool_uses,
}
```

- [ ] **Step 4: Add the idempotent schema and persistence field**

Add `prompt_count` to the initial `CREATE TABLE files` and an existing-database migration:

```sql
ALTER TABLE files
  ADD COLUMN IF NOT EXISTS prompt_count INT NOT NULL DEFAULT 0;
```

Extend the `files` insert/update in `backend/ingest.py` with `prompt_count`, populated as:

```python
"prompt_count": int(parsed.get("prompt_count", 0)),
```

- [ ] **Step 5: Run focused and native regression tests**

Run:

```bash
python3 -m pytest tests/test_parse.py tests/test_parse_codex.py tests/test_ingest.py -q
```

Expected: all pass; existing record values are unchanged and every parse result includes prompt count.

- [ ] **Step 6: Commit the shared contract**

```bash
git add backend/parse_common.py backend/schema.sql backend/ingest.py tests/test_parse.py tests/test_ingest.py
git commit -m $'feat: add additive prompt parser contract\n\nCo-Authored-By: GPT-5.6 Sol <noreply@openai.com>'
```

---

### Task 2: Parse core GPT usage from Claude-format JSONL

**Files:**
- Create: `backend/parse_claude.py`
- Create: `tests/test_parse_claude.py`
- Create: `fixtures/parser/claude_gpt_single_turn.jsonl`
- Create: `fixtures/parser/claude_gpt_streaming_merge.jsonl`
- Create: `fixtures/parser/claude_gpt_iterations.jsonl`
- Modify: `backend/parse.py:24-29,475-575`
- Modify: `tests/test_parse.py` Claude detection section

**Interfaces:**
- Consumes: `_make_usage_record(...) -> dict` from Task 1.
- Produces: `backend.parse_claude.parse(file_key: str, blob: bytes) -> dict` with `records`, `ctx_turns`, `turn_count`, `prompt_count`, `rate_limit_hits`, `tool_uses`, and optional `project_display_name`.
- Produces: `parse.parse_file` dispatches detected Claude lines to `parse_claude.parse`.

- [ ] **Step 1: Create a real GPT-shaped fixture and failing dispatch test**

Create `fixtures/parser/claude_gpt_single_turn.jsonl` with a queue record, one substantive user line, and one assistant line whose message contains:

```json
{
  "model": "gpt-5.6-sol",
  "content": [{"type": "text", "text": "hi"}],
  "usage": {
    "input_tokens": 12,
    "cache_creation_input_tokens": 5,
    "cache_read_input_tokens": 900,
    "output_tokens": 7
  }
}
```

Keep `sessionId`, `uuid`, `requestId`, timestamps, and `cwd: "/root/codexmeter"` in the enclosing Claude Code records.

Create `tests/test_parse_claude.py`:

```python
from pathlib import Path

from backend import parse, pricing

FIX = Path(__file__).parents[1] / "fixtures" / "parser"


def _parse(name: str) -> dict:
    return parse.parse_file(
        f"-root-codexmeter/s/{name}",
        (FIX / name).read_bytes(),
    )


def test_gpt_single_turn_dispatches_and_prices_exactly():
    out = _parse("claude_gpt_single_turn.jsonl")
    assert out["prompt_count"] == 1
    assert out["turn_count"] == 1
    assert out["project_display_name"] == "/root/codexmeter"
    rec = out["records"][0]
    assert rec["model"] == "gpt-5.6-sol"
    assert (rec["fresh_tokens"], rec["cache_creation_tokens"],
            rec["cache_read_tokens"], rec["output_tokens"]) == (12, 5, 900, 7)
    assert rec["cost_usd"] == round(pricing.compute_cost(
        "gpt-5.6-sol", fresh=12, create=5, read=900, output=7
    ), 6)
```

- [ ] **Step 2: Run the single-turn test and verify the old skip behavior**

Run:

```bash
python3 -m pytest tests/test_parse_claude.py::test_gpt_single_turn_dispatches_and_prices_exactly -v
```

Expected: FAIL because `UnsupportedTranscriptError` is still raised.

- [ ] **Step 3: Implement the adapter's usage primitives**

In `backend/parse_claude.py`, port and type the following from Claudit: `_merge_usage_max`, `_flatten_usage`, `_usage_ctx_input`, `_to_dt`, and a `_ClaudeWalk` state holding `seen_request`, ordered assistant events, substantive prompt lines, seen user UUIDs, and `cwd` candidates.

Use this projection for each merged assistant event:

```python
ctx_input = _usage_ctx_input(usage)
record = _make_usage_record(
    file_key,
    line_num,
    _to_dt(timestamp),
    uuid or None,
    model or "(unknown)",
    (
        int(usage.get("input_tokens", 0) or 0),
        int(usage.get("cache_creation_input_tokens", 0) or 0),
        int(usage.get("cache_read_input_tokens", 0) or 0),
        int(usage.get("output_tokens", 0) or 0),
    ),
    text_chars=text_chars,
    reply_latency_s=reply_latency_s,
    ctx_input=ctx_input,
    long_context=ctx_input > pricing.LONG_CONTEXT_THRESHOLD,
)
```

Skip `<synthetic>` assistant records. Merge repeated non-empty `requestId` usage recursively by numeric maximum; retain requestless records individually.

- [ ] **Step 4: Implement prompt boundaries and context turns**

Port Claudit's substantive user filtering exactly: ignore blank strings, instrumentation prefixes, interrupt markers, and replayed UUIDs for latency mutation. Count substantive prompts and build context turns from the last usage record before each next user boundary, dropping zero-input turns.

Return a `project_display_name` only when the set of non-empty absolute `cwd` values has exactly one member:

```python
cwd_values = {v for v in walk.cwd_values if v.startswith("/")}
if len(cwd_values) == 1:
    result["project_display_name"] = next(iter(cwd_values))
```

- [ ] **Step 5: Dispatch without changing native precedence**

Import `parse_claude` beside `parse_codex`. Keep the existing scan order and replace only:

```python
if fmt == "claude":
    raise UnsupportedTranscriptError(...)
```

with:

```python
if fmt == "claude":
    return parse_claude.parse(file_key, blob)
```

Update the old unsupported-format tests to assert successful parsing; retain tests proving a leading unknown line and unkeyed file-history record still identify the format.

- [ ] **Step 6: Add streaming and iteration fixtures/tests**

Create `claude_gpt_streaming_merge.jsonl` with two assistant records sharing `requestId`, increasing usage, and distinct line numbers. Create `claude_gpt_iterations.jsonl` whose usage contains two iterations.

Assert:

```python
def test_streaming_request_is_one_max_merged_record():
    out = _parse("claude_gpt_streaming_merge.jsonl")
    assert len(out["records"]) == 1
    assert out["records"][0]["output_tokens"] == 11


def test_iterations_sum_billing_and_peak_context():
    rec = _parse("claude_gpt_iterations.jsonl")["records"][0]
    assert rec["fresh_tokens"] == 30
    assert rec["ctx_input"] == 920
```

Use fixture numbers whose iteration sums and peak are unambiguous; assert every billed field, not only the two shown above.

- [ ] **Step 7: Run core parser tests and commit**

```bash
python3 -m pytest tests/test_parse_claude.py tests/test_parse.py tests/test_parse_codex.py -q
git add backend/parse_claude.py backend/parse.py tests/test_parse_claude.py tests/test_parse.py fixtures/parser/claude_gpt_single_turn.jsonl fixtures/parser/claude_gpt_streaming_merge.jsonl fixtures/parser/claude_gpt_iterations.jsonl
git commit -m $'feat: parse GPT usage from Claude transcripts\n\nCo-Authored-By: GPT-5.6 Sol <noreply@openai.com>'
```

---

### Task 3: Port full Claudit metric semantics

**Files:**
- Modify: `backend/parse_claude.py`
- Modify: `tests/test_parse_claude.py`
- Copy and adapt: `/root/claudit/fixtures/parser/{tool_success,tool_error,tool_unmatched,edit_churn,write_churn,replayed_prompt,interrupt_list_content,rate_limit,list_content_anchor,mixed_timestamps}.jsonl`
- Create corresponding files under: `fixtures/parser/claude_*.jsonl`

**Interfaces:**
- Consumes: core `_ClaudeWalk` and `parse(...)` from Task 2.
- Produces: full `tool_uses`, `rate_limit_hits`, `text_chars`, `reply_latency_s`, churn, prompt, and context behavior.

- [ ] **Step 1: Copy focused source fixtures under explicit names**

Copy each listed Claudit fixture to a `claude_`-prefixed destination in Codexmeter. Where a fixture has an assistant model, replace only that model with `gpt-5.6-sol`; retain UUIDs, request IDs, timestamps, tool IDs, and content exactly so the source behavior remains recognizable.

- [ ] **Step 2: Write failing table-driven fidelity tests**

Add tests covering:

```python
def test_tool_success_and_error_resolution():
    ok = _parse("claude_tool_success.jsonl")["tool_uses"][0]
    bad = _parse("claude_tool_error.jsonl")["tool_uses"][0]
    assert ok["is_error"] is False
    assert bad["is_error"] is True
    assert (bad["lines_added"], bad["lines_deleted"]) == (0, 0)


def test_unmatched_tool_result_keeps_unknown_error_state():
    tool = _parse("claude_tool_unmatched.jsonl")["tool_uses"][0]
    assert tool["is_error"] is None


def test_replay_and_interrupt_do_not_corrupt_latency_or_prompts():
    replay = _parse("claude_replayed_prompt.jsonl")
    interrupted = _parse("claude_interrupt_list_content.jsonl")
    assert replay["prompt_count"] == 1
    assert interrupted["prompt_count"] == 1
    assert interrupted["records"][-1]["reply_latency_s"] is None
```

Add exact assertions for Edit/Write line counts, visible text characters, list-content anchors, mixed timestamps, and rate-limit content capped at 500 characters.

- [ ] **Step 3: Implement content and tool extraction**

Port `_content_metrics` and `_tool_churn` into the adapter. Accept `tool_use` and `server_tool_use`; create one tool row per distinct block ID, with fallback key `requestId:line:idx` for idless blocks.

Use Codexmeter's tool row keys:

```python
{
    "file_key": file_key,
    "line_num": line_num,
    "idx": idx,
    "ts": parsed_timestamp,
    "tool_name": name,
    "tool_call_id": tool_id,
    "is_error": None,
    "lines_added": added,
    "lines_deleted": deleted,
}
```

Resolve user `tool_result.tool_use_id` after the walk and remove the internal `tool_call_id` exactly as `parse_common._resolve_tool_errors` does.

- [ ] **Step 4: Implement replay, interrupt, latency, and rate-limit semantics**

Port Claudit's instrumentation prefixes and `[Request interrupted by user` marker. A replayed user UUID may count only on its first line and never mutate the latency anchor. Consume an anchor on the next real assistant usage record; discard negative deltas.

Recognize rate-limit assistant error records only when `isApiErrorMessage is True`, `error == "rate_limit"`, and joined text contains `out of extra usage`, storing:

```python
{"line": line_num, "ts": timestamp, "content": joined[:500]}
```

- [ ] **Step 5: Run fidelity and regression tests**

```bash
python3 -m pytest tests/test_parse_claude.py tests/test_parse.py tests/test_parse_codex.py -q
```

Expected: all full-metric tests pass; existing formats remain green.

- [ ] **Step 6: Commit the full adapter**

```bash
git add backend/parse_claude.py tests/test_parse_claude.py fixtures/parser/claude_*.jsonl
git commit -m $'feat: preserve Claude transcript metrics\n\nCo-Authored-By: GPT-5.6 Sol <noreply@openai.com>'
```

---

### Task 4: Make Claude archive keys first-class ingest candidates

**Files:**
- Modify: `backend/ingest.py:101-277,335-358,882-942`
- Modify: `tests/test_ingest.py` Claude transcript section
- Modify: `fixtures/parser/claude_transcript.jsonl` only if the old synthetic model must remain separate from the new GPT fixture

**Interfaces:**
- Produces: `_Transcript(NamedTuple)` with `obj`, `project_id`, `session_id`, and `is_main`.
- Produces: `_candidate(obj) -> _Transcript | None` for native and Claude layouts.
- Consumes: optional `parsed["project_display_name"]` and required `parsed["prompt_count"]`.

- [ ] **Step 1: Replace skip expectations with failing ingest expectations**

Rewrite the existing foreign-layout tests to expect insertion:

```python
def test_claude_layout_main_and_subagent_are_ingested(fresh_db, mini_r2_env):
    _put_claude(mini_r2_env, "-root-codexmeter/ses-1/ses-1.jsonl")
    _put_claude(
        mini_r2_env,
        "-root-codexmeter/ses-1/data/subagents/agent-a1.jsonl",
    )
    result = ingest.run_ingest(trigger="manual")
    assert result["error"] is None
    assert result["skipped"] == 0
    assert result["inserted"] == 7
    with db.viz_conn() as c:
        rows = c.execute(
            "SELECT project_id, session_id, is_main "
            "FROM files WHERE project_id = '-root-codexmeter' "
            "ORDER BY is_main DESC"
        ).fetchall()
    assert rows == [
        ("-root-codexmeter", "ses-1", True),
        ("-root-codexmeter", "ses-1", False),
    ]
```

Keep the existing native fixture count explicit so the test proves extension rather than replacement.

- [ ] **Step 2: Add compressed, display-label, idempotence, and retry tests**

Use `lzma.compress(fixture.read_bytes())` for a `.jsonl.xz` mirror object. Assert main detection strips the full suffix. Assert the project's `display_name` is `/root/codexmeter`, a second unchanged ingest inserts/reparses zero, and a monkeypatched deterministic parser failure creates no `files` row and is retried next run.

- [ ] **Step 3: Normalize both key layouts**

Introduce:

```python
class _Transcript(NamedTuple):
    obj: object
    project_id: str
    session_id: str
    is_main: bool


def _jsonl_stem(key: str) -> str | None:
    for suffix in (".jsonl.xz", ".jsonl"):
        if key.endswith(suffix):
            return key.rsplit("/", 1)[-1][:-len(suffix)]
    return None
```

Implement `_candidate(obj)`:

- native: `sessions/<project>/<session>/.../wire.jsonl[.xz]`, preserving `"/subagents/" not in key`;
- Claude: non-`sessions`, at least three components, JSONL suffix, `project_id = parts[0]`, `session_id = parts[1]`, and `is_main = stem == session_id`;
- otherwise `None`.

Change `_Scan` to hold `transcripts: list[_Transcript]` and `marker_items`; remove the foreign count. Keep marker discovery unchanged.

- [ ] **Step 4: Plan normalized candidates through the existing pipeline**

Change `_plan_work` to iterate `_Transcript` values instead of reparsing key positions. Its `todo` items remain `(obj, proj, project_id, session_id, is_main, stored)`, so fetching and persistence do not fork by format.

Count every candidate in `listed` and `seen_keys`. `skipped` now counts only future `UnsupportedTranscriptError` outcomes, not Claude-layout objects.

- [ ] **Step 5: Apply parser-derived display labels without changing identity**

In `_persist_one`, before calling `_persist`, copy the project dict and apply a non-empty parser label:

```python
project = dict(proj)
display_name = parsed.get("project_display_name") if parsed else None
if isinstance(display_name, str) and display_name.startswith("/"):
    project["display_name"] = display_name
```

Pass the copy to `_persist`. Never change `project_id` or `session_id` from transcript content.

- [ ] **Step 6: Run ingest, canonicalization, and rollup tests**

```bash
python3 -m pytest tests/test_ingest.py tests/test_ingest_codex.py tests/test_project_identifiers.py tests/test_project_display_label.py -q
```

Expected: all pass; Claude objects insert, native objects remain identical, and derived-state tests stay green.

- [ ] **Step 7: Commit normalized ingest**

```bash
git add backend/ingest.py tests/test_ingest.py fixtures/parser/claude_transcript.jsonl
git commit -m $'feat: ingest Claude archive layouts\n\nCo-Authored-By: GPT-5.6 Sol <noreply@openai.com>'
```

If the existing fixture was not modified, omit it from `git add`.

---

### Task 5: Expose substantive prompt count

**Files:**
- Modify: `backend/api_sessions.py:98-123`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `files.prompt_count` from Task 1.
- Produces: `/api/context-growth/session/{session_id}` includes `total_prompts: int` while preserving `total_turns`.

- [ ] **Step 1: Write the failing API contract test**

In `tests/test_api.py`, extend the context-growth session test:

```python
response = client.get(f"/api/context-growth/session/{session_id}")
assert response.status_code == 200
body = response.json()
assert body["total_turns"] >= 0
assert body["total_prompts"] >= body["total_turns"]
```

Set the selected fixture row's `prompt_count` to a known value and assert that exact value rather than relying only on the inequality.

- [ ] **Step 2: Run the focused test and observe the missing field**

```bash
python3 -m pytest tests/test_api.py -k context_growth_session -v
```

Expected: FAIL with missing `total_prompts`.

- [ ] **Step 3: Extend the query and response additively**

Select `prompt_count` beside `turn_count`, unpack it, and return:

```python
"total_turns": count,
"total_prompts": prompt_count,
```

Do not rename or remove any existing response field.

- [ ] **Step 4: Run API tests and commit**

```bash
python3 -m pytest tests/test_api.py tests/test_dashboard_token_contract.py -q
git add backend/api_sessions.py tests/test_api.py
git commit -m $'feat: expose session prompt counts\n\nCo-Authored-By: GPT-5.6 Sol <noreply@openai.com>'
```

---

### Task 6: Extend the browser Inspector parser

**Files:**
- Modify: `src/parser.js:131-173,704-708,710-790`
- Modify: `tests/test_parser_js_usage_records.py`
- Modify: `tests/test_parser_js_stats.py`
- Modify: `tests/test_parser_js_mirror.py`

**Interfaces:**
- Produces: `_parseClaude(blob) -> {events, meta_events, ctx_turns, turn_count, tool_uses, prompt_count}`.
- Produces: `_buildClaudeCtxTurns(events, meta) -> list` using substantive user-line boundaries and `assistant_usage` context.
- Consumes: existing `window.rateForModel`, `window.resolveModelRate`, and Codexmeter model-rate table.
- Preserves: `_parseLegacy`, `_parseKimiCode`, and their dispatch outputs.

- [ ] **Step 1: Add a failing browser dispatch test using the shared fixture**

Load `src/parser.js` through the existing Node harness and parse `claude_gpt_single_turn.jsonl`. Assert:

```javascript
const parsed = window.parseTranscript(blob);
if (parsed.prompt_count !== 1) throw new Error("wrong prompt count");
const usage = parsed.meta_events.filter(e => e.type === "assistant_usage");
if (usage.length !== 1) throw new Error("missing usage");
if (usage[0].model !== "gpt-5.6-sol") throw new Error("wrong model");
```

Also run one existing Kimi fixture through the same harness and retain its exact assertions.

- [ ] **Step 2: Extend format detection after native markers**

Add:

```javascript
function _isClaudeObject(obj) {
  return typeof obj.sessionId === "string"
      || ["file-history-snapshot", "file-history-delta"].includes(obj.type);
}
```

Call it only after Kimi/native detection checks, returning `"claude"`.

- [ ] **Step 3: Adapt Claudit's event parser under a private function**

Port `/root/claudit/src/parser.js:6-289` into `_parseClaude`, not onto `window.parseTranscript`. Retain user, assistant text/thinking, tool call/result, agent-spawn, request max-merge, and call/result linking behavior. Remove Claudit's model-rate declarations and use Codexmeter's existing globals. Mark each emitted `user_message` with `substantive: true|false` using the same blank/instrumentation/interrupt rules as the backend, and increment `substantivePromptCount` only for `true` records.

Build browser context turns explicitly, then normalize Claudit's return:

```javascript
function _buildClaudeCtxTurns(events, meta) {
  const boundaries = events
    .filter(e => e.type === "user_message" && e.substantive !== false)
    .map(e => e.line)
    .sort((a, b) => a - b);
  const usage = meta
    .filter(e => e.type === "assistant_usage")
    .sort((a, b) => a.line - b.line);
  const chosen = [];
  let boundaryIndex = 0;
  let last = null;
  for (const item of usage) {
    while (boundaryIndex < boundaries.length
           && boundaries[boundaryIndex] <= item.line) {
      if (last) chosen.push(last);
      last = null;
      boundaryIndex++;
    }
    last = item;
  }
  if (last) chosen.push(last);
  let previous = 0;
  let turnIndex = 0;
  return chosen.flatMap((item) => {
    const input = window.usageCtxInput(item.usage);
    if (input <= 0) return [];
    const turn = {
      idx: ++turnIndex,
      ts: item.ts || "",
      line: item.line,
      input,
      output: Number(item.usage.output_tokens || 0),
      delta: input - previous,
    };
    previous = input;
    return [turn];
  });
}

const ctxTurns = _buildClaudeCtxTurns(events, meta);
return {
  events,
  meta_events: meta,
  ctx_turns: ctxTurns,
  turn_count: ctxTurns.length,
  tool_uses: events.filter(e => e.type === "tool_call" || e.type === "agent_spawn"),
  prompt_count: substantivePromptCount,
};
```

Keep `assistant_usage` fields in the shape consumed by the existing `window.asUsageRecord` boundary; extend that selector with one Claude branch before its current `status_update` branch:

```javascript
if (m && m.type === "assistant_usage" && m.usage) {
  return {
    ts: m.ts,
    wire_model: m.model,
    token_usage: {
      input_other: Number(m.usage.input_tokens || 0),
      input_cache_creation: Number(m.usage.cache_creation_input_tokens || 0),
      input_cache_read: Number(m.usage.cache_read_input_tokens || 0),
      output: Number(m.usage.output_tokens || 0),
    },
  };
}
```

This keeps every consumer behind one usage-shape selector instead of teaching each consumer both formats.

- [ ] **Step 4: Dispatch additively and preserve existing stats**

Change only the dispatcher:

```javascript
if (fmt === "claude") return _parseClaude(blob);
if (fmt === "kimi-code") return _parseKimiCode(blob);
return _parseLegacy(blob);
```

Extend `window.usageCtxInput` with the iterations peak branch from Claudit while retaining current Kimi field handling.

- [ ] **Step 5: Run browser parser and mirror tests**

```bash
python3 -m pytest tests/test_parser_js_usage_records.py tests/test_parser_js_stats.py tests/test_parser_js_mirror.py -q
npx eslint src/parser.js
```

Expected: Claude fixture produces GPT usage and full events; every existing browser test passes; Python/JS rate keys remain identical.

- [ ] **Step 6: Commit the Inspector extension**

```bash
git add src/parser.js tests/test_parser_js_usage_records.py tests/test_parser_js_stats.py tests/test_parser_js_mirror.py
git commit -m $'feat: inspect Claude-format GPT transcripts\n\nCo-Authored-By: GPT-5.6 Sol <noreply@openai.com>'
```

---

### Task 7: Document, invalidate, verify, and integrate

**Files:**
- Modify: `AGENTS.md:19-34,56-58,107-117`
- Modify: `README.md` parser/ingest documentation
- Modify: `src/parser.js:7` (`window.PARSER_VERSION`)
- Modify at deployment: `/root/codexmeter/.env` (`PARSER_VERSION`), only after reading its current value

**Interfaces:**
- Consumes: all prior tasks.
- Produces: documented supported layouts, parser version `10`, clean full verification, pushed `master`.

- [ ] **Step 1: Update documentation and parser versions**

Replace the “foreign transcripts are skipped” documentation with both supported layouts and the dedicated adapter. State that formerly skipped files have no rows and ingest automatically.

Set:

```javascript
window.PARSER_VERSION = "10";
```

Read `/root/codexmeter/.env`, verify its current parser version is older than `10`, then change only that key to:

```dotenv
PARSER_VERSION=10
```

Do not overwrite or commit the deployment `.env`.

- [ ] **Step 2: Run focused suites before the full gate**

```bash
python3 -m pytest tests/test_parse_claude.py tests/test_parse.py tests/test_parse_codex.py tests/test_ingest.py tests/test_ingest_codex.py tests/test_api.py -q
```

Expected: all pass.

- [ ] **Step 3: Run the repository's canonical pre-push gate**

```bash
/root/codexmeter/scripts/pre-push
```

Expected: Python tests, lint, types, JavaScript checks, and secrecy checks all pass. Preserve all output artifacts if the script creates any.

- [ ] **Step 4: Commit documentation/version changes**

```bash
git add AGENTS.md README.md src/parser.js
git commit -m $'docs: document Claude-format Codex ingestion\n\nCo-Authored-By: GPT-5.6 Sol <noreply@openai.com>'
```

- [ ] **Step 5: Review the whole branch against the design**

Compare `4e98e09..HEAD` against `docs/superpowers/specs/2026-08-11-claude-format-codex-ingest-design.md`. Confirm every goal has code and a test, every new helper has one responsibility, and no native parser behavior was replaced.

- [ ] **Step 6: Verify git integration state and push once**

```bash
git status --short
git push
git rev-list --count @{u}..HEAD
git status --short
```

Expected: both status commands are empty and ahead count is `0`.

- [ ] **Step 7: Exercise the deployed ingest**

Restart only the project service after inspecting it:

```bash
systemctl status codexmeter.service --no-pager
systemctl restart codexmeter.service
systemctl status codexmeter.service --no-pager
```

Then verify the next ingest run reports Claude-layout objects as inserted/reparsed rather than skipped, and verify at least one persisted record has `model = 'gpt-5.6-sol'` and `project_id = '-root-codexmeter'`. Do not print transcript content or credentials.
