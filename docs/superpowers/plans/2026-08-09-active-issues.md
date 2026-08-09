# Active Issues Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve GitHub issues #1, #3, #4, #5, and #6 with truthful shared model, bucket, and token-type contracts.

**Architecture:** Keep the backend authoritative for model prices and token semantics, with explicit mirrors only where the no-build browser requires them. Normalize API rows once before rendering, then make all dashboard calculations consume normalized metadata rather than hardcoded token fields. Preserve the documented lack of a model dimension in tool rollups and represent unknown attribution honestly.

**Tech Stack:** Python 3.13, FastAPI, PostgreSQL/psycopg, React 18 JSX via in-browser Babel, plain JavaScript executed by Node in tests, pytest.

## Global Constraints

- Support both Kimi wire transcripts and Codex rollout transcripts.
- Luna pricing is `fresh=0.20`, `create=0.25`, `read=0.02`, `output=1.20` USD per million tokens.
- Sol, Terra, and Luna each have a 272,000-token context window.
- `tool_uses` and `tool_rollup` remain without a model dimension.
- Missing optional token fields mean zero; present non-finite token fields are invalid.
- Reasoning output is a subset of output and must not be added to token or cost totals.
- Cache-write tokens are an independent billed addend.
- Bump `PARSER_VERSION` from 8 to 9 for parser/pricing invalidation.
- Every commit includes `Co-authored-by: GPT-5.6 Sol <noreply@openai.com>`.
- Issue implementation commits include `Closes #N` for every issue they resolve.
- Unrelated bugs become new GitHub issues and are not fixed here.

---

### Task 1: Unify model pricing and frontend model constants (#1)

**Files:**
- Modify: `tests/test_parser_js_mirror.py`
- Modify: `tests/test_parse_codex.py`
- Modify: `tests/test_pricing.py`
- Create: `tests/test_codex_model_constants.py`
- Modify: `backend/pricing.py`
- Modify: `backend/parse_codex.py`
- Modify: `backend/schema.sql`
- Modify: `backend/.env.example`
- Modify: `.env` (ignored deployment configuration)
- Modify: `src/parser.js`
- Modify: `src/synthetic-data.js`
- Modify: `src/dashboard-charts-extra.jsx`
- Modify: `src/dashboard-charts.jsx`
- Modify: `src/app.jsx`

**Interfaces:**
- Produces: `pricing.MODEL_RATES: dict[str, dict[str, float]]` containing all six canonical display labels.
- Produces: `pricing.DEFAULT_RATES` equal to `MODEL_RATES["gpt-5.6-luna"]`.
- Preserves: `pricing.resolve(model) -> Resolution` and browser `window.rateForModel(model)` matching semantics.

- [ ] **Step 1: Write failing pricing and constant tests**

Replace the split-table assertion with a unified-table contract and add a static/Node probe that asserts the Codex context limits, unknown primary defaults, parser version, and synthetic prices:

```python
def test_every_parser_model_is_in_the_mirrored_rate_table():
    assert set(pricing.MODEL_RATES) == {
        "kimi-k3", "kimi-k2-7-code", "kimi-k2-6",
        "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
    }
    assert pricing.DEFAULT_RATES is pricing.MODEL_RATES["gpt-5.6-luna"]

def test_luna_keeps_the_eighty_percent_price_cut():
    assert pricing.MODEL_RATES["gpt-5.6-luna"] == {
        "fresh": .20, "create": .25, "read": .02, "output": 1.20,
    }
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `python3 -m pytest tests/test_pricing.py tests/test_parse_codex.py tests/test_parser_js_mirror.py tests/test_codex_model_constants.py -q`

Expected: failures show Codex rates remain outside the mirror, the default is Kimi, stale frontend constants remain, and the synthetic Luna values are too high.

- [ ] **Step 3: Implement the unified canonical table**

Store display labels in `MODEL_RATES`, normalize both the candidate and each key while matching, and mirror all entries in `src/parser.js`:

```python
MODEL_RATES = {
    "kimi-k3": {"fresh": 3.00, "create": 0.00, "read": .30, "output": 15.00},
    "kimi-k2-7-code": {"fresh": .95, "create": 0.00, "read": .19, "output": 4.00},
    "kimi-k2-6": {"fresh": .95, "create": 0.00, "read": .16, "output": 4.00},
    "gpt-5.6-sol": {"fresh": 5.00, "create": 6.25, "read": .50, "output": 30.00},
    "gpt-5.6-terra": {"fresh": 2.00, "create": 2.50, "read": .20, "output": 12.00},
    "gpt-5.6-luna": {"fresh": .20, "create": .25, "read": .02, "output": 1.20},
}
DEFAULT_RATES = MODEL_RATES["gpt-5.6-luna"]
```

Update every `CODEX_MODEL_RATES` reader to `MODEL_RATES`, use 272,000 for all three Codex context entries, use `unknown` for empty primary-model states, correct the synthetic rate table, and set the schema default to `gpt-5.6-sol` with an idempotent `ALTER COLUMN SET DEFAULT`.

Set `window.PARSER_VERSION`, `backend/.env.example`, and the ignored `.env` to `9`.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_pricing.py tests/test_parse_codex.py tests/test_parser_js_mirror.py tests/test_codex_model_constants.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit issue #1**

```bash
git add backend/pricing.py backend/parse_codex.py backend/schema.sql backend/.env.example \
  src/parser.js src/synthetic-data.js src/dashboard-charts-extra.jsx \
  src/dashboard-charts.jsx src/app.jsx tests/test_pricing.py \
  tests/test_parse_codex.py tests/test_parser_js_mirror.py \
  tests/test_codex_model_constants.py
git commit -m "fix: unify Codex model constants" \
  -m "Closes #1" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 2: Make backend tool/model identity truthful (#6)

**Files:**
- Modify: `tests/test_api.py`
- Create: `tests/test_project_identifiers.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_ingest.py`
- Modify: `backend/api.py`
- Modify: `backend/cache.py`
- Modify: `backend/ingest.py`
- Modify: `backend/db.py`
- Modify: `backend/schema.sql`
- Modify: `backend/.env.example`
- Modify: `.env` (ignored deployment configuration)

**Interfaces:**
- Consumes: `pricing.resolve(model).kind` from Task 1.
- Produces: tool-error rows with `model=<requested exact model>` or `model="unknown"` when unfiltered.
- Produces: `CODEXMETER_TIMING` and `CODEXMETER_WARM_CACHE` operational switches.

- [ ] **Step 1: Write failing endpoint and identifier tests**

Add probe rows, rebuild the rollup, and assert:

```python
assert client.get("/api/tool-usage?range=3650d&model=gpt-5.6-sol").json()["buckets"]
assert client.get("/api/tool-usage?range=3650d&model=not-real").json()["buckets"] == []
rows = client.get("/api/tool-error-rate?range=3650d").json()["buckets"]
assert {row["model"] for row in rows} == {"unknown"}
```

Add a source scan requiring `codexmeter.*`, `CODEXMETER_TIMING`, and
`CODEXMETER_WARM_CACHE`, with no `kimimeter` project identifiers left under
`backend/`.

- [ ] **Step 2: Run focused tests and verify RED**

Run: `python3 -m pytest tests/test_api.py tests/test_project_identifiers.py tests/test_ingest.py -q`

Expected: Codex model filtering is empty, unfiltered error rows say Kimi, and old project identifiers are found.

- [ ] **Step 3: Implement catalog validation and identity renames**

Replace `_ONLY_MODELS` with a helper based on exact pricing resolution:

```python
def _known_model(model: str | None) -> bool:
    return bool(model) and pricing.resolve(model).kind == "exact"
```

Use `model or "unknown"` for the SQL label. Rename the logger hierarchy and
environment switches, update tests/configuration, and rename stale database
documentation/messages to codexmeter without changing SQL behavior.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_api.py tests/test_project_identifiers.py tests/test_ingest.py -q`

Expected: all selected tests pass.

- [ ] **Step 5: Commit issue #6**

```bash
git add backend/api.py backend/cache.py backend/ingest.py backend/db.py \
  backend/schema.sql backend/.env.example tests/test_api.py \
  tests/test_project_identifiers.py tests/conftest.py tests/test_ingest.py
git commit -m "fix: use Codex models in tool endpoints" \
  -m "Closes #6" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 3: Honor server aggregation width in dashboard charts (#3)

**Files:**
- Create: `tests/test_dashboard_bin_size.py`
- Modify: `src/app.jsx`

**Interfaces:**
- Produces: `dashboardBinMs(range: {start: number, end: number}, bucketS?: number) -> number`.

- [ ] **Step 1: Write a failing Node-driven bin test**

Extract the real pure helper from `src/app.jsx` and assert:

```javascript
dashboardBinMs({start: 0, end: 6 * 86400000}, 21600) === 21600000
dashboardBinMs({start: 0, end: 6 * 86400000}, 3600) === 3600000
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `python3 -m pytest tests/test_dashboard_bin_size.py -q`

Expected: failure because the helper/clamp does not exist.

- [ ] **Step 3: Extract and apply the clamped picker**

```javascript
function dashboardBinMs(range, bucketS) {
  const span = range.end - range.start;
  const niceBins = [
    60_000, 5 * 60_000, 15 * 60_000, 30 * 60_000,
    3_600_000, 6 * 3_600_000, 12 * 3_600_000, 24 * 3_600_000,
  ];
  let binMs = niceBins[0];
  for (const candidate of niceBins) {
    if (span / candidate < 100) break;
    binMs = candidate;
  }
  return Math.max(binMs, Number(bucketS || 0) * 1000);
}
```

Use this helper in `Dashboard`; all panels continue receiving one shared width.

- [ ] **Step 4: Run focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_dashboard_bin_size.py -q`

Expected: pass.

- [ ] **Step 5: Commit issue #3**

```bash
git add src/app.jsx tests/test_dashboard_bin_size.py
git commit -m "fix: honor dashboard aggregation width" \
  -m "Closes #3" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 4: Render optional token types generically (#4, #5)

**Files:**
- Modify: `tests/test_token_type_rendering.py`
- Modify: `tests/test_payload_fields_are_used.py`
- Create: `tests/test_dashboard_token_contract.py`
- Modify: `backend/api_dashboard.py`
- Modify: `src/app.jsx`
- Modify: `src/dashboard-charts.jsx`

**Interfaces:**
- Produces: dashboard `token_types: Array<{field, label, total, rate}>`.
- Produces: normalized events containing every field named by `token_types`.
- Produces: `computeTokenBreakdown(events, tokenTypes)` and `tokenTotal(events, tokenTypes)`.

- [ ] **Step 1: Write failing backend metadata tests**

Assert that metadata follows response-wide suppression and encodes subset/addend semantics:

```python
meta = _token_type_metadata(_drop_zero_token_types([_entry(
    input_tokens=10, output_tokens=4, cache_write_tokens=3,
    reasoning_output_tokens=2,
)]))
assert {m["field"] for m in meta} == {
    "input_tokens", "output_tokens", "cache_write_tokens",
    "reasoning_output_tokens",
}
assert next(m for m in meta if m["field"] == "cache_write_tokens")["total"] is True
assert next(m for m in meta if m["field"] == "reasoning_output_tokens")["total"] is False
```

- [ ] **Step 2: Write failing Node-driven frontend contract tests**

Drive the real pure functions from `src/app.jsx` with payloads that omit cache
read, include cache write/reasoning, and contain an explicit `NaN`:

```javascript
const shape = backendDashToShape(payloadWithoutCacheRead);
tokenTotal(shape.events, shape.tokenTypes) === input + output;
tokenTotal(payloadWithCacheWrite.events, tokenTypes) === input + output + cacheWrite;
// reasoning is rendered but excluded from the total
expectInvalid(() => backendDashToShape(payloadWithNaN));
```

Also assert the panel descriptor list includes every metadata type and exactly
one Total Tokens panel.

- [ ] **Step 3: Run focused tests and verify RED**

Run: `python3 -m pytest tests/test_token_type_rendering.py tests/test_payload_fields_are_used.py tests/test_dashboard_token_contract.py -q`

Expected: metadata is absent, cache write/reasoning have no reader, missing cache read yields `NaN`, and invalid values are silently swallowed.

- [ ] **Step 4: Implement backend token descriptors**

Replace the bare field tuple with descriptor definitions while preserving a
derived `TOKEN_TYPE_FIELDS` tuple for SQL/suppression tests:

```python
TOKEN_TYPES = (
    {"field": "input_tokens", "label": "Input Tokens", "total": True, "rate": "fresh"},
    {"field": "output_tokens", "label": "Output Tokens", "total": True, "rate": "output"},
    {"field": "cache_read_tokens", "label": "Cache Read", "total": True, "rate": "read"},
    {"field": "cache_write_tokens", "label": "Cache Write", "total": True, "rate": "create"},
    {"field": "reasoning_output_tokens", "label": "Reasoning Output", "total": False, "rate": None},
)
TOKEN_TYPE_FIELDS = tuple(item["field"] for item in TOKEN_TYPES)
```

Return only descriptors whose fields survived suppression.

- [ ] **Step 5: Implement generic normalization, totals, breakdown, and panels**

Use a strict optional-number helper:

```javascript
function optionalFiniteNumber(object, field) {
  if (!Object.prototype.hasOwnProperty.call(object, field)) return 0;
  const value = Number(object[field]);
  if (!Number.isFinite(value)) throw new TypeError(`Invalid numeric field: ${field}`);
  return value;
}
```

Map every metadata field onto each event, derive total addends from `total`,
create a time-series descriptor per visible type, and give reasoning output a
token series without a second cost row. Add Cache Write and Reasoning Output
colors plus a deterministic fallback. Remove their payload allowlist entries.

Change the chart bin reducer from `value || 0` to missing-only handling so an
invalid computed number cannot masquerade as zero.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run: `python3 -m pytest tests/test_token_type_rendering.py tests/test_payload_fields_are_used.py tests/test_dashboard_token_contract.py -q`

Expected: all selected tests pass.

- [ ] **Step 7: Commit issues #4 and #5**

```bash
git add backend/api_dashboard.py src/app.jsx src/dashboard-charts.jsx \
  tests/test_token_type_rendering.py tests/test_payload_fields_are_used.py \
  tests/test_dashboard_token_contract.py
git commit -m "fix: render optional token types generically" \
  -m "Closes #4" -m "Closes #5" \
  -m "Co-authored-by: GPT-5.6 Sol <noreply@openai.com>"
```

### Task 5: Full verification and remote reconciliation

**Files:**
- Verify only; edit only if a scoped regression test exposes an incomplete issue fix.

**Interfaces:**
- Consumes: all previous task outputs.

- [ ] **Step 1: Run formatting/static checks**

Run: `git diff --check HEAD~4..HEAD`

Expected: no output, exit 0.

- [ ] **Step 2: Run the complete suite**

Run: `python3 -m pytest tests/ -q`

Expected: 0 failed; the pre-change baseline was 323 passed.

- [ ] **Step 3: Verify commit trailers and scope**

Run: `git log -5 --format='%H%n%B%n---'`

Expected: every new commit has the required coauthor; issue commits contain the correct `Closes` trailers.

- [ ] **Step 4: Re-query active GitHub issues and inspect the final diff**

Confirm #1, #3, #4, #5, and #6 are each represented by a closing commit and no newly discovered unrelated defect was silently fixed.

- [ ] **Step 5: Push the verified default branch**

Run: `git push origin master`

Expected: fast-forward success. GitHub closes #1, #3, #4, #5, and #6 from the default-branch commit trailers.
