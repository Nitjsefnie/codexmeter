"""Unit tests for window.asUsageRecord and its three consumers (issue #14).

Three frontend paths still read claudit's record shape — meta type
"assistant_usage" with a `usage` object — which this parser never emits (it
emits "status_update" with `token_usage`), so each silently matched nothing:

  * src/context-growth-view.jsx computeTurnStats — the Inspector's Context
    growth tab rendered "No turns…" for every transcript;
  * src/app.jsx txToDashData — liveData was always null, so the
    drop-a-file → dashboard path produced nothing;
  * src/app.jsx loadFiles — session-id resolution scanned for a sessionId
    on usage records that the parser never sets, always falling back to the
    filename.

The fix introduces ONE shared selector, window.asUsageRecord in
src/parser.js, and routes all three consumers (plus computeSessionStats)
through it. Site 3's scan was REMOVED, not translated: the parser emits no
sessionId anywhere (one file ≈ one session), so the filename is the only
id source — the tagging loop now marks usage records with it via the
shared selector.

These tests drive the REAL src/parser.js plus the real computeTurnStats /
txToDashData function bodies (extracted from the .jsx sources — node cannot
parse JSX, but both functions are plain JS) through node, matching the
repo's no-build-step rule (same approach as test_parser_js_stats.py).
There is no browser here, so React rendering itself is NOT exercised.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"
CTX_VIEW_JSX = ROOT / "src" / "context-growth-view.jsx"
APP_JSX = ROOT / "src" / "app.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node_probe():
    """Run the real parser.js + the two extracted view functions under node.

    The fixture's status_updates carry wire_model "k3" so expected costs are
    pinned to the kimi-k3 rates (fresh 3.00, create 0.00, read 0.30,
    output 15.00 per 1M) with no dependence on the date-based model ladder.
    """
    script = f"""
      global.window = {{}};
      const fs = require('fs');
      eval(fs.readFileSync({str(PARSER_JS)!r}, 'utf8'));

      // The two consumers live in .jsx files node cannot parse wholesale,
      // but both function bodies are plain JS — extract and eval them
      // verbatim so the test drives the real source, not a copy.
      function extract(path, startMarker, endMarker) {{
        const src = fs.readFileSync(path, 'utf8');
        const start = src.indexOf(startMarker);
        const end = src.indexOf(endMarker);
        if (start < 0 || end < 0 || end <= start) {{
          throw new Error('extraction failed: ' + startMarker);
        }}
        return src.slice(start, end);
      }}
      eval(extract({str(CTX_VIEW_JSX)!r},
        'function computeTurnStats', 'function ContextGrowthView'));
      window.shortModelName = (m) => m || 'unknown';
      eval(extract({str(APP_JSX)!r}, 'function txToDashData', 'function App'));

      const su = (ts, line, tu, extra) => Object.assign(
        {{ type: 'status_update', ts, line, wire_model: 'k3',
           token_usage: tu }}, extra || {{}});

      // ── selector ────────────────────────────────────────────────
      const valid = window.asUsageRecord(
        su(150, 5, {{ input_other: 1000, input_cache_creation: 500,
                     input_cache_read: 2000, output: 100 }}));
      const rejects = [
        {{ type: 'status_update', ts: 1, line: 2 }},           // no token_usage
        {{ type: 'assistant_usage', ts: 1, line: 2,           // claudit shape
           usage: {{ input_tokens: 5 }} }},
        {{ type: 'turn_end', ts: 1, line: 2 }},
        null, undefined, 'status_update', 42,
      ].map((x) => window.asUsageRecord(x) === null);
      const legacyModel = window.asUsageRecord(
        {{ type: 'status_update', ts: 1000, line: 1,
           token_usage: {{ input_other: 1 }} }});               // no wire_model
      const noTs = window.asUsageRecord(
        {{ type: 'status_update', line: 1,
           token_usage: {{ input_other: 1 }} }});
      const fallbackTs = window.asUsageRecord(
        {{ type: 'status_update', line: 1,
           token_usage: {{ input_other: 1 }} }}, 1000);
      const odd = window.asUsageRecord(
        su(1, 1, {{ input_other: '5', input_cache_creation: null,
                   input_cache_read: NaN, output: undefined }}));

      // ── computeTurnStats (Context growth tab) ───────────────────
      const tx = {{
        events: [
          {{ type: 'user_message', ts: 100, line: 1, detail: 'first' }},
          {{ type: 'user_message', ts: 200, line: 10, detail: 'second' }},
          {{ type: 'user_message', ts: 300, line: 20, detail: '   ' }},
        ],
        meta: [
          su(150, 5, {{ input_other: 1000, input_cache_creation: 500,
                       input_cache_read: 2000, output: 100 }}),
          su(160, 6, {{ input_other: 0, input_cache_creation: 0,
                       input_cache_read: 4000, output: 200 }}),
          su(250, 15, {{ input_other: 0, input_cache_creation: 0,
                        input_cache_read: 0, output: 5 }}),   // refusal
          su(260, 16, {{ input_other: 500, input_cache_creation: 0,
                        input_cache_read: 4500, output: 50 }}),
          {{ type: 'turn_end', ts: 261, line: 17 }},
        ],
      }};
      const rows = computeTurnStats(tx);

      // ── txToDashData (drop-a-file → dashboard) ──────────────────
      const dash = txToDashData({{
        events: [
          {{ type: 'user_message', ts: 100, line: 1, detail: 'q1',
             sessionId: 'fileA' }},
        ],
        meta: [
          su(150, 5, {{ input_other: 1000, input_cache_creation: 500,
                       input_cache_read: 2000, output: 100 }},
             {{ sessionId: 'fileA' }}),
          su(160, 6, {{ input_other: 0, input_cache_creation: 0,
                       input_cache_read: 4000, output: 200 }},
             {{ sessionId: 'fileB' }}),
          {{ type: 'rate_limit', ts: 170, line: 7, content: 'hit' }},
        ],
      }});
      const dashEmpty = txToDashData({{
        events: [], meta: [{{ type: 'turn_end', ts: 1, line: 1 }}],
      }});

      // ── site 3: parser emits no sessionId; tagging loop ────────
      const wire = JSON.stringify({{
        type: 'usage.record', time: 150, model: 'kimi-code/k3',
        usage: {{ inputOther: 10, inputCacheCreation: 0,
                 inputCacheRead: 20, output: 5 }},
      }});
      const parsed = window.parseTranscript(wire);
      const parsedMetas = parsed.meta_events.map((m) => ({{
        type: m.type, hasSessionId: 'sessionId' in m,
      }}));
      const sid = 'transcript1';
      let tagged = 0;
      for (const m of parsed.meta_events) {{
        if (window.asUsageRecord(m) && !m.sessionId) {{
          m.sessionId = sid; tagged++;
        }}
      }}
      const taggedRec = window.asUsageRecord(parsed.meta_events[0]);

      console.log(JSON.stringify({{
        valid, rejects,
        legacyModel: legacyModel && legacyModel.model,
        noTs: noTs && noTs.model,
        fallbackTs: fallbackTs && fallbackTs.model,
        odd,
        rows, dash, dashEmpty,
        parsedMetas, tagged, taggedSid: taggedRec && taggedRec.sessionId,
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module", name="js")
def _js_probe():
    return _node_probe()


# ── shared selector ────────────────────────────────────────────────────

def test_as_usage_record_normalises_status_update(js):
    v = js["valid"]
    assert v["line"] == 5
    assert v["ts"] == 150
    assert v["tsMs"] == 150_000
    assert v["model"] == "kimi-k3"  # wire_model canonicalised
    assert v["input"] == 1000
    assert v["create"] == 500
    assert v["read"] == 2000
    assert v["output"] == 100
    assert v["ctx"] == 3500


def test_as_usage_record_rejects_non_usage_records(js):
    assert js["rejects"] == [True] * 7


def test_as_usage_record_model_ladder(js):
    # No wire_model: the date ladder decides; an unstamped record falls back
    # to k2-7-code unless a fallback ts (the session's first event) says older.
    assert js["legacyModel"] == "kimi-k2-6"
    assert js["noTs"] == "kimi-k2-7-code"
    assert js["fallbackTs"] == "kimi-k2-6"


def test_as_usage_record_odd_fields_never_nan(js):
    odd = js["odd"]
    for key in ("input", "create", "read", "output", "ctx"):
        assert odd[key] == 0, key


# ── site 1: computeTurnStats ───────────────────────────────────────────

def test_compute_turn_stats_produces_turns(js):
    # The regression that proves the Context growth tab populates: two real
    # turns from status_update records, where the claudit shape gave zero.
    rows = js["rows"]
    assert len(rows) == 2
    # Turn 1 takes the LAST usage record before the next boundary (line 6,
    # not line 5); turn 2 skips the ctx==0 refusal at line 15.
    assert rows[0]["line"] == 6
    assert rows[0]["ctx"] == 4000
    assert rows[0]["input"] == 0
    assert rows[0]["cacheRead"] == 4000
    assert rows[0]["output"] == 200
    assert rows[0]["model"] == "kimi-k3"
    assert rows[0]["delta"] is None
    assert rows[1]["line"] == 16
    assert rows[1]["ctx"] == 5000
    assert rows[1]["delta"] == 1000
    assert [r["turnNum"] for r in rows] == [1, 2]


# ── site 2: txToDashData ───────────────────────────────────────────────

def test_tx_to_dash_data_builds_events_per_session(js):
    dash = js["dash"]
    assert dash is not None
    events = dash["events"]
    assert len(events) == 2
    by_sid = {e["session_id"]: e for e in events}
    assert set(by_sid) == {"fileA", "fileB"}
    a, b = by_sid["fileA"], by_sid["fileB"]
    assert a["ts"] == 150_000
    assert a["input_tokens"] == 1000
    assert a["output_tokens"] == 100
    assert a["cache_read"] == 2000
    assert a["ctx"] == 3500
    assert a["model"] == "kimi-k3"
    # Hand-worked at kimi-k3 rates: 1000*3.00 + 500*0.00 + 2000*0.30
    # + 100*15.00 = 5100 per 1M; fileB: 4000*0.30 + 200*15.00 = 4200 per 1M.
    assert a["cost_usd"] == pytest.approx(0.0051)
    assert b["cost_usd"] == pytest.approx(0.0042)
    assert [e["turn_index"] for e in events] == [0, 0]  # one turn per session
    assert len(dash["limitHits"]) == 1
    assert dash["range"]["start"] < dash["range"]["end"]


def test_tx_to_dash_data_null_without_usage_records(js):
    assert js["dashEmpty"] is None


# ── site 3: session-id resolution ──────────────────────────────────────

def test_parser_emits_no_session_id(js):
    # The fact site 3 now encodes: no record carries a sessionId, so the
    # filename is the only session-id source for dropped files.
    assert js["parsedMetas"] == [{"type": "status_update", "hasSessionId": False}]


def test_tagging_loop_marks_usage_records_with_filename_sid(js):
    assert js["tagged"] == 1
    assert js["taggedSid"] == "transcript1"
