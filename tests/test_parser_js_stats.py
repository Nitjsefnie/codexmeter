"""Unit tests for window.computeSessionStats / window.usageCtxInput (issue #10).

The Inspector's SessionHeader consumes an OBJECT of session stats, but the
two window globals the view actually calls were never defined in this repo
at all (a claudit carry-over), so the whole single-session view crashed.
The only stats builder src/parser.js shipped at the time returned a
human-readable STRING and would have thrown on stats.hitRate.toFixed(1);
it had no consumer and was removed in #16.

These tests drive the REAL src/parser.js through node — no npm, no build
step, matching the repo's in-browser-Babel, no-toolchain rule (same
approach as test_parser_js_mirror.py). They pin:

  * computeSessionStats' key set — the contract SessionHeader reads;
  * its values on a hand-built status_update/token_usage fixture, with the
    cost worked out by hand from the kimi-k3 rates so a pricing regression
    cannot hide behind a re-derived expectation;
  * usageCtxInput's sum over codexmeter's three input buckets and its
    no-NaN behaviour on missing/odd fields;
  * the batch_size/batch_index annotation the parser adds when one
    assistant message carries several toolCalls — the wire format's only
    parallel-tool-call representation, which the parallelBatches stat
    counts.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# Every key SessionHeader (src/app.jsx) reads off the stats object, plus the
# supporting aggregates. A missing key renders "undefined" in the header —
# or throws, for hitRate.toFixed(1).
EXPECTED_KEYS = {
    "firstTs", "lastTs", "turns", "userMsgs", "toolCalls", "toolResults",
    "errorResults", "parallelBatches", "parallelCalls", "toolCounts",
    "fresh", "create", "read", "output", "totalInput", "hitRate", "cost",
}


def _node_probe():
    """Run the real parser.js under node against a hand-built fixture.

    The fixture's status_updates carry wire_model "k3" so the expected cost
    below is pinned to the kimi-k3 rates (fresh 3.00, create 0.00,
    read 0.30, output 15.00 per 1M) with no dependence on the date-based
    model ladder.
    """
    script = f"""
      global.window = {{}};
      const src = require('fs').readFileSync({str(PARSER_JS)!r}, 'utf8');
      eval(src);

      const events = [
        {{ type: 'user_message', ts: 990, line: 1 }},
        {{ type: 'tool_call', ts: 1000, line: 2, tool_name: 'Read',
           batch_size: 2, batch_index: 1 }},
        {{ type: 'tool_call', ts: 1000, line: 2, tool_name: 'Bash',
           batch_size: 2, batch_index: 2 }},
        {{ type: 'tool_result', ts: 1001, line: 3, is_error: false }},
        {{ type: 'tool_result', ts: 1002, line: 4, is_error: true }},
        {{ type: 'assistant_text', ts: 1003, line: 5 }},
      ];
      const meta = [
        {{ type: 'status_update', ts: 1000, line: 6, wire_model: 'k3',
           token_usage: {{ input_other: 1000, input_cache_creation: 500,
                           input_cache_read: 2000, output: 100 }} }},
        {{ type: 'status_update', ts: 1060, line: 7, wire_model: 'k3',
           token_usage: {{ input_other: 0, input_cache_creation: 0,
                           input_cache_read: 4000, output: 200 }} }},
        {{ type: 'turn_end', ts: 1061, line: 8 }},
      ];
      const stats = window.computeSessionStats(events, meta);

      // One assistant message carrying two toolCalls — the wire format's
      // parallel-tool-call representation.
      const wire = JSON.stringify({{
        type: 'context.append_message', time: 1000000,
        message: {{
          role: 'assistant',
          content: [{{ type: 'text', text: 'hi' }}],
          toolCalls: [
            {{ type: 'function', id: 'c1', name: 'Read', arguments: '{{}}' }},
            {{ type: 'function', id: 'c2', name: 'Bash', arguments: '{{}}' }},
          ],
        }},
      }});
      const parsed = window.parseTranscript(wire);
      const parsedStats = window.computeSessionStats(
        parsed.events, parsed.meta_events);

      console.log(JSON.stringify({{
        stats: stats,
        uci: {{
          full: window.usageCtxInput({{ input_other: 1,
            input_cache_creation: 2, input_cache_read: 3 }}),
          partial: window.usageCtxInput({{ input_other: 7 }}),
          empty: window.usageCtxInput({{}}),
          nullU: window.usageCtxInput(null),
          undefinedU: window.usageCtxInput(undefined),
          odd: window.usageCtxInput({{ input_other: '5',
            input_cache_creation: null, input_cache_read: NaN }}),
        }},
        batchEvents: parsed.events
          .filter((e) => e.type === 'tool_call')
          .map((e) => ({{ id: e.tool_call_id,
                          batch_size: e.batch_size,
                          batch_index: e.batch_index }})),
        parsedParallelBatches: parsedStats.parallelBatches,
        parsedParallelCalls: parsedStats.parallelCalls,
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


def test_compute_session_stats_returns_object_with_expected_keys(js):
    stats = js["stats"]
    assert isinstance(stats, dict)
    assert EXPECTED_KEYS <= set(stats), \
        f"missing keys: {EXPECTED_KEYS - set(stats)}"


def test_compute_session_stats_event_counts(js):
    stats = js["stats"]
    assert stats["firstTs"] == 990_000   # epoch ms, for SessionHeader's dur
    assert stats["lastTs"] == 1_003_000
    assert stats["userMsgs"] == 1
    assert stats["toolCalls"] == 2
    assert stats["toolResults"] == 2
    assert stats["errorResults"] == 1
    assert stats["parallelBatches"] == 1
    assert stats["parallelCalls"] == 2
    assert stats["toolCounts"] == {"Read": 1, "Bash": 1}


def test_compute_session_stats_token_totals(js):
    stats = js["stats"]
    assert stats["turns"] == 2  # one turn per status_update with token_usage
    assert stats["fresh"] == 1000
    assert stats["create"] == 500
    assert stats["read"] == 6000
    assert stats["output"] == 300
    assert stats["totalInput"] == 7500
    assert stats["hitRate"] == pytest.approx(80.0)


def test_compute_session_stats_cost_uses_per_record_k3_rates(js):
    # m1: 1000*3.00 + 500*0.00 + 2000*0.30 + 100*15.00 = 5100 per 1M
    # m2:    0*3.00 +   0*0.00 + 4000*0.30 + 200*15.00 = 4200 per 1M
    assert js["stats"]["cost"] == pytest.approx(0.0051 + 0.0042)


def test_usage_ctx_input_sums_the_three_input_buckets(js):
    assert js["uci"]["full"] == 6
    assert js["uci"]["partial"] == 7


def test_usage_ctx_input_missing_or_odd_fields_never_nan(js):
    # A NaN would cross the JSON boundary as null, and null != 0 — so
    # equality with 0 alone pins "no NaN".
    for key in ("empty", "nullU", "undefinedU", "odd"):
        assert js["uci"][key] == 0, key


def test_multi_toolcall_message_is_annotated_as_a_parallel_batch(js):
    assert js["batchEvents"] == [
        {"id": "c1", "batch_size": 2, "batch_index": 1},
        {"id": "c2", "batch_size": 2, "batch_index": 2},
    ]
    assert js["parsedParallelBatches"] == 1
    assert js["parsedParallelCalls"] == 2
