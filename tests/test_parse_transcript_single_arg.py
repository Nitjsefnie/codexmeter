"""Regression tests for the dead cross-file dedup removal (issue #15).

loadFiles built a seenUuids set and passed it as a SECOND argument to
window.parseTranscript, which takes one parameter — the set stayed
empty, before/after deltas were always zero, and the UI reported a
"uniq recs" figure implying a cross-file dedup that never ran. The repo
owner decided to REMOVE the dead machinery, not implement the dedup.

These tests pin the removal:

  * the real src/parser.js parseTranscript keeps its single-parameter
    signature (driven through node, same approach as
    test_parser_js_stats.py);
  * src/app.jsx carries no seenUuids / dupedRecs bookkeeping and no
    "uniq recs" status text, so the UI cannot imply a dedup that isn't
    happening (and the eslint no-unused-vars error for dupedRecs stays
    gone).
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PARSER_JS = ROOT / "src" / "parser.js"
APP_JSX = ROOT / "src" / "app.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def test_parse_transcript_takes_one_argument():
    script = f"""
      global.window = {{}};
      const fs = require('fs');
      eval(fs.readFileSync({str(PARSER_JS)!r}, 'utf8'));
      console.log(JSON.stringify({{ arity: window.parseTranscript.length }}));
    """
    proc = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["arity"] == 1


def test_load_files_has_no_dedup_bookkeeping():
    src = APP_JSX.read_text()
    assert "seenUuids" not in src
    assert "dupedRecs" not in src
    assert "uniq recs" not in src
