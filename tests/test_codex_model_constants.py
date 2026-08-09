"""Cross-frontend behavior for Codex model constants.

These probes execute the shipped JavaScript with Node. They assert the first
consumer-visible result that depends on each table: generated event cost,
context percentage denominator, and dominant-model fallback.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from backend import pricing

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node(script: str) -> object:
    proc = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True,
        text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_synthetic_codex_events_use_backend_rates():
    rows = _node("""
      global.window = {};
      eval(require('fs').readFileSync('src/synthetic-data.js', 'utf8'));
      const first = {};
      for (const event of window.generateSyntheticData().events) {
        if (event.model.startsWith('gpt-') && !first[event.model]) {
          first[event.model] = event;
        }
      }
      console.log(JSON.stringify(first));
    """)
    assert set(rows) == {"gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"}
    for model, row in rows.items():
        rates = pricing.MODEL_RATES[model]
        want = (
            row["input_tokens"] * rates["fresh"]
            + row["output_tokens"] * rates["output"]
            + row["cache_read"] * rates["read"]
        ) / 1_000_000
        assert row["cost_usd"] == pytest.approx(want, rel=1e-12), model


def test_context_caps_match_models_the_parsers_emit():
    caps = _node(r"""
      global.window = { dashboardTheme: {}, humanFmt: (v) => String(v) };
      const src = require('fs').readFileSync(
        'src/dashboard-charts-extra.jsx', 'utf8');
      const prefix = src.slice(0, src.indexOf('function buildSessionTurns'));
      const models = [
        'kimi-k3', 'kimi-k2-7-code', 'kimi-k2-6',
        'gpt-5.6-sol', 'gpt-5.6-terra', 'gpt-5.6-luna', 'unknown'
      ];
      const out = eval(prefix + '\n;Object.fromEntries(models.map(m => [m, capForModel(m)]))');
      console.log(JSON.stringify(out));
    """)
    assert caps == {
        "kimi-k3": 256_000,
        "kimi-k2-7-code": 256_000,
        "kimi-k2-6": 256_000,
        "gpt-5.6-sol": 272_000,
        "gpt-5.6-terra": 272_000,
        "gpt-5.6-luna": 272_000,
        "unknown": 272_000,
    }


def test_an_empty_model_count_has_a_truthful_unknown_primary():
    result = _node(r"""
      const src = require('fs').readFileSync('src/dashboard-charts.jsx', 'utf8');
      const start = src.indexOf('function dominantModel');
      if (start < 0) throw new Error('dominantModel helper missing');
      const end = src.indexOf('\nfunction ', start + 1);
      eval(src.slice(start, end));
      console.log(JSON.stringify({
        empty: dominantModel({}),
        counted: dominantModel({'gpt-5.6-luna': 2, 'gpt-5.6-sol': 5}),
      }));
    """)
    assert result == {"empty": "unknown", "counted": "gpt-5.6-sol"}
