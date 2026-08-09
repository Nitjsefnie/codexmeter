"""Execute the dashboard's real token normalization and arithmetic in Node."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _probe() -> dict:
    script = r"""
      global.window = {
        shortModelName: (m) => m || 'unknown',
        dashboardCol: {
          inputTokens: '#1', outputTokens: '#2', cacheReadTokens: '#3',
          cacheWriteTokens: '#4', reasoningOutputTokens: '#5', totalTokens: '#6'
        },
        rateForModel: () => ({fresh: 5, output: 30, out: 30, read: .5, create: 6.25}),
      };
      const app = require('fs').readFileSync('src/app.jsx', 'utf8');
      const start = app.indexOf('function optionalFiniteNumber');
      if (start < 0) throw new Error('optionalFiniteNumber helper missing');
      const topBar = app.indexOf('\nfunction TopBar', start);
      const breakdown = app.indexOf('function computeTokenBreakdown');
      const breakdownEnd = app.indexOf('\nfunction TokenBreakdownPanel', breakdown);
      const api = eval(
        app.slice(start, topBar)
        + '\n' + app.slice(breakdown, breakdownEnd)
        + '\n;({backendDashToShape, effectiveTokenTypes, tokenTotal, tokenPanelDefinitions, computeTokenBreakdown})'
      );

      const base = {
        bucket_s: 3600,
        hourly: [{
          hour: '2026-08-09T00:00:00Z', model: 'gpt-5.6-sol',
          input_tokens: 100, output_tokens: 50, cost_usd: 1,
          requests: 1, session_count: 1,
        }],
        token_types: [
          {field: 'input_tokens', label: 'Input Tokens', total: true, rate: 'fresh'},
          {field: 'output_tokens', label: 'Output Tokens', total: true, rate: 'output'},
        ],
      };
      const missing = api.backendDashToShape(base);

      const richPayload = JSON.parse(JSON.stringify(base));
      richPayload.hourly[0].cache_write_tokens = 3;
      richPayload.hourly[0].reasoning_output_tokens = 7;
      richPayload.token_types.push(
        {field: 'cache_write_tokens', label: 'Cache Write', total: true, rate: 'create'},
        {field: 'reasoning_output_tokens', label: 'Reasoning Output', total: false, rate: null},
      );
      const rich = api.backendDashToShape(richPayload);
      const breakdownRows = api.computeTokenBreakdown(rich.events, rich.tokenTypes).rows;

      let invalid = null;
      try {
        api.backendDashToShape({
          ...base,
          hourly: [{...base.hourly[0], input_tokens: 'not-a-number'}],
        });
      } catch (error) {
        invalid = error.name + ': ' + error.message;
      }

      const charts = require('fs').readFileSync('src/dashboard-charts.jsx', 'utf8');
      const finiteStart = charts.indexOf('function finiteSeriesValue');
      if (finiteStart < 0) throw new Error('finiteSeriesValue helper missing');
      const finiteEnd = charts.indexOf('\nfunction ', finiteStart + 1);
      eval(charts.slice(finiteStart, finiteEnd));
      let chartInvalid = null;
      try { finiteSeriesValue({_t: NaN}, '_t'); }
      catch (error) { chartInvalid = error.name + ': ' + error.message; }

      console.log(JSON.stringify({
        missingTotal: api.tokenTotal(missing.events, missing.tokenTypes),
        missingCacheRead: missing.events[0].cache_read,
        explicitEmptyTypes: api.effectiveTokenTypes([]),
        missingMetadataTypes: api.effectiveTokenTypes(undefined).map(t => t.field),
        richTotal: api.tokenTotal(rich.events, rich.tokenTypes),
        richFields: Object.keys(rich.events[0]).filter(k => k.endsWith('_tokens')).sort(),
        panels: api.tokenPanelDefinitions(rich.tokenTypes).map(p => [p.field, p.label]),
        reasoningCost: breakdownRows.find(r => r.field === 'reasoning_output_tokens').cost,
        cacheWriteCost: breakdownRows.find(r => r.field === 'cache_write_tokens').cost,
        invalid,
        chartMissing: finiteSeriesValue({}, 'missing'),
        chartInvalid,
      }));
    """
    proc = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True,
        text=True, timeout=60, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_optional_types_drive_totals_panels_costs_and_validation():
    out = _probe()
    assert out["missingTotal"] == 150
    assert out["missingCacheRead"] == 0
    assert out["explicitEmptyTypes"] == []
    assert out["missingMetadataTypes"] == [
        "input_tokens", "output_tokens", "cache_read_tokens",
    ]
    assert out["richTotal"] == 153
    assert out["richFields"] == [
        "cache_write_tokens", "input_tokens", "output_tokens",
        "reasoning_output_tokens",
    ]
    assert out["panels"] == [
        ["input_tokens", "Input Tokens"],
        ["output_tokens", "Output Tokens"],
        ["cache_write_tokens", "Cache Write"],
        ["reasoning_output_tokens", "Reasoning Output"],
    ]
    assert out["reasoningCost"] is None
    assert out["cacheWriteCost"] == pytest.approx(3 * 6.25 / 1_000_000)
    assert out["invalid"].startswith("TypeError: Invalid numeric field input_tokens")
    assert out["chartMissing"] == 0
    assert out["chartInvalid"].startswith("TypeError: Invalid series value _t")
