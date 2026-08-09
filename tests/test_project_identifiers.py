"""Operational identifiers expose the current project name at runtime."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_codexmeter_timing_switch_enables_the_codexmeter_logger_tree():
    env = dict(os.environ)
    env["CODEXMETER_TIMING"] = "1"
    env["KIMIMETER_TIMING"] = "0"
    script = """
import json
from backend import api, cache, ingest
print(json.dumps({
    "timing": api.TIMING_ON,
    "api": api.log.name,
    "cache": cache.log.name,
    "ingest": ingest.log.name,
}))
"""
    proc = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "timing": True,
        "api": "codexmeter.api",
        "cache": "codexmeter.cache",
        "ingest": "codexmeter.ingest",
    }
