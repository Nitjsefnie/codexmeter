"""src/parser.js MIRRORS backend/pricing.py.

The in-browser Inspector prices dropped transcripts with its own copy of the
rate table and its own matcher. If the two drift, the drag-drop view and the
dashboard disagree on cost for the same file — and the drift is silent, because
each half is internally consistent. That is exactly what happened after the
rate-lookup anchoring work: backend/pricing.py grew an anchored _match_key
while src/parser.js kept an unanchored `model.includes(key)`, so a hypothetical
"kimi-k3-mini" was priced at the flagged default by one half and at K3's ~3x
rates by the other.

Parity is asserted by driving the REAL src/parser.js through node — no npm, no
build step, matching the repo's in-browser-Babel, no-toolchain rule. parser.js
is plain JS (only the views are JSX), so node can run it directly.

Two directions are covered, because either one alone leaves a hole:

  * the literal tables, enumerated — catches a rate changed on one side, or a
    model added to or removed from one side;
  * the matching behaviour, driven id by id — catches the semantics diverging
    while the tables still agree, which is the case that actually bit.

claudit's copy of this test also pins the dated-rate epochs
(test_parser_js_exposes_the_same_rate_epochs). kimimeter has no dated rates —
no DATED_RATES, no RATE_EPOCHS on either side — so that test has no analogue
here and is deliberately not ported rather than invented.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)

# The four billed buckets. parser.js also returns claudit-compatible aliases
# (out/c5/c1h) that app.jsx reads; those are checked separately.
BUCKETS = ("fresh", "create", "read", "output")

# Ids whose outcome the anchoring work turned on. Every one of these merely
# CONTAINS a table key, so the old substring matcher billed each at that key's
# rates; all of them must now reach the default on BOTH sides.
NEAR_MISS_IDS = [
    "kimi-k3-mini",
    "kimi-k2-6-turbo",
    "kimi-k2-7-code-fast",
    "kimi-k2-60",
    "x-kimi-k2-7-code-y",
    "kimi-code/k3",       # a raw wire id must NOT be quietly repaired
    "not-a-real-model",
    "gpt-5",
    "",
    None,
]

# Suffix forms that still name a known model, applied to every table key so a
# model added to the Python table is driven through the JS matcher for free.
EXACT_SUFFIXES = ["", "[1m]", "@latest", "-20260601", "-202606"]

# Forms whose shape cannot be generated from a key by concatenation.
IRREGULAR_IDS = [
    "KIMI-K3",            # case
    "  kimi-k3  ",        # padding
    "kimi-k2.7-code",     # dotted version separator
    "Kimi-K2.7-Code[1m]",  # all three at once
]


def _cases():
    ids = [key + suffix for key in pricing.MODEL_RATES for suffix in EXACT_SUFFIXES]
    return ids + IRREGULAR_IDS + NEAR_MISS_IDS


CASES = _cases()


def _node_probe():
    """Run the real parser.js under node; return its table and its verdicts.

    MODEL_RATES / DEFAULT_RATES are module-local consts, so the source is
    eval'd with a trailing expression in the SAME scope to read them out —
    deliberately, rather than exporting them on `window`, which would put a
    global in the shipped parser that only a test consumes.
    """
    script = f"""
      global.window = {{}};
      const src = require('fs').readFileSync({str(PARSER_JS)!r}, 'utf8');
      const tables = eval(src + '\\n;({{ MODEL_RATES, DEFAULT_RATES }})');
      const cases = {json.dumps(CASES)};
      console.log(JSON.stringify({{
        table: tables.MODEL_RATES,
        keys: Object.keys(tables.MODEL_RATES),
        defaults: tables.DEFAULT_RATES,
        results: cases.map((m) => ({{ model: m, rates: window.rateForModel(m) }})),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


@pytest.fixture(scope="module")
def js():
    return _node_probe()


def test_parser_js_rate_table_matches_backend_pricing(js):
    """The literal tables, both directions: same models, same rates, same
    order (the matcher walks the table most-specific-first, so order is part
    of the contract, not cosmetic).
    """
    assert js["keys"] == list(pricing.MODEL_RATES)
    for key, py_rates in pricing.MODEL_RATES.items():
        js_rates = js["table"][key]
        assert set(js_rates) == set(BUCKETS), key
        for bucket in BUCKETS:
            assert js_rates[bucket] == pytest.approx(py_rates[bucket]), \
                f"{key}: {bucket}"


def test_parser_js_default_rates_name_the_same_model(js):
    for bucket in BUCKETS:
        assert js["defaults"][bucket] == pytest.approx(
            pricing.DEFAULT_RATES[bucket]
        ), bucket


def test_parser_js_rate_for_model_matches_backend_resolve(js):
    """The matching semantics, id by id. A table that agrees but a matcher
    that does not still bills the same transcript two different ways.
    """
    results = {json.dumps(r["model"]): r["rates"] for r in js["results"]}
    assert len(results) == len(CASES), "duplicate case ids"

    for model in CASES:
        got = results[json.dumps(model)]
        want = pricing.resolve(model)
        for bucket in BUCKETS:
            assert got[bucket] == pytest.approx(want.rates[bucket]), \
                f"{model!r} (py kind={want.kind}, key={want.key}): {bucket}"


def test_parser_js_prices_every_near_miss_id_at_the_flagged_default(js):
    """The near misses land on the default on both sides — not merely on the
    same rates as each other. Pinned separately from the parity test so a
    table edit that made the default equal K3's rates could not hide it.

    One blind spot, stated rather than papered over: DEFAULT_RATES IS the
    kimi-k2-6 entry, and rateForModel returns rates without a kind, so a JS
    matcher that wrongly accepted "kimi-k2-6-turbo" as an exact kimi-k2-6
    would be indistinguishable here from one that defaulted it. That
    divergence is numerically harmless — the same four rates either way —
    and closing it would mean exposing a resolution kind the frontend has
    no reader for. Every near miss whose wrong key carries DIFFERENT rates
    (the kimi-k3 and kimi-k2-7-code families) is caught outright.
    """
    results = {json.dumps(r["model"]): r["rates"] for r in js["results"]}
    for model in NEAR_MISS_IDS:
        assert pricing.resolve(model).kind == "default", f"python: {model!r}"
        got = results[json.dumps(model)]
        for bucket in BUCKETS:
            assert got[bucket] == pytest.approx(pricing.DEFAULT_RATES[bucket]), \
                f"js: {model!r}: {bucket}"


def test_parser_js_exposes_the_claudit_compatible_rate_aliases(js):
    """app.jsx reads r.out; parser.js's cost folds read r.output. Both come
    off one lookup, so an id that returned the bare table dict (as the empty
    id used to) silently made every r.out NaN.
    """
    for r in js["results"]:
        rates = r["rates"]
        assert set(rates) == {"fresh", "create", "read", "output",
                              "out", "c5", "c1h"}, r["model"]
        assert rates["out"] == rates["output"], r["model"]
        assert rates["c5"] == rates["create"], r["model"]
        assert rates["c1h"] == 0, r["model"]
