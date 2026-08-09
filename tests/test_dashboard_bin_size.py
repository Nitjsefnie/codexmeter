"""Dashboard display bins cannot be finer than backend aggregates."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "src" / "app.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def test_display_bin_honors_server_bucket_and_data_span():
    script = rf"""
      const src = require('fs').readFileSync({str(APP)!r}, 'utf8');
      const start = src.indexOf('function dashboardBinMs');
      if (start < 0) throw new Error('dashboardBinMs helper missing');
      const end = src.indexOf('\nfunction Dashboard', start);
      eval(src.slice(start, end));
      const day = 86400000;
      console.log(JSON.stringify({{
        wideServer: dashboardBinMs({{start: 0, end: 6 * day}}, 21600),
        hourlyServer: dashboardBinMs({{start: 0, end: 6 * day}}, 3600),
        shortData: dashboardBinMs({{start: 0, end: 90 * 60000}}, 300),
        offline: dashboardBinMs({{start: 0, end: 6 * day}}, null),
      }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], cwd=ROOT, capture_output=True,
        text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "wideServer": 6 * 3_600_000,
        "hourlyServer": 3_600_000,
        "shortData": 5 * 60_000,
        "offline": 3_600_000,
    }
