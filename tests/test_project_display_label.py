"""Unit tests for the Cost-by-Project display-name join (issue #13).

The chart labelled bars with the raw `cost_by_project.project` id (a
`wd_<hash>` string here) while the project picker directly above showed
`display_name` for the same project. The fix is a client-side join in
src/app.jsx: Dashboard now receives the (full, unpaginated) projects
list, builds project_id -> display_name, and labels bars through the
pure helper window-level `projectDisplayLabel`, falling back to the raw
id on a miss so no row is silently dropped.

Key check (verified by reading backend/api.py, not by a live DB):
cost_by_project is folded from `SELECT u.project_id ... FROM
usage_rollup` (api.py cost_by_project query) and /api/projects returns
`p.project_id` from the projects table — both are the ingest-written
project id, one key space. Rows that cannot join keep the raw id: the
synthetic "Other (N projects)" and "unknown" rows, and any project the
projects endpoint renames to '<unresolved>'.

These tests drive the REAL helper (extracted verbatim from app.jsx —
node cannot parse JSX, but the function is plain JS) through node,
matching the repo's no-build-step rule (same approach as
test_parser_js_stats.py). The React wiring is pinned by source-level
assertions only; rendering is unverified without a browser.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_JSX = ROOT / "src" / "app.jsx"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node not available"
)


def _node_probe():
    script = f"""
      global.window = {{}};
      const fs = require('fs');
      const src = fs.readFileSync({str(APP_JSX)!r}, 'utf8');
      const start = src.indexOf('function projectDisplayLabel');
      const end = src.indexOf('function Dashboard');
      if (start < 0 || end < 0 || end <= start) {{
        throw new Error('projectDisplayLabel extraction failed');
      }}
      eval(src.slice(start, end));

      const names = {{ 'wd_abc123': 'My Project', 'wd_def456': 'Other App' }};
      console.log(JSON.stringify({{
        hit: projectDisplayLabel('wd_abc123', names),
        miss: projectDisplayLabel('wd_999', names),
        otherRow: projectDisplayLabel('Other (3 projects)', names),
        unknownRow: projectDisplayLabel('unknown', names),
        nullMap: projectDisplayLabel('wd_abc123', null),
        emptyMap: projectDisplayLabel('wd_abc123', {{}}),
        emptyName: projectDisplayLabel('wd_abc123', {{ 'wd_abc123': '' }}),
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


def test_known_project_resolves_to_display_name(js):
    assert js["hit"] == "My Project"


def test_unmatched_project_falls_back_to_raw_id(js):
    # A rollup row absent from the projects list is never dropped or
    # blanked — it keeps its raw identifier.
    assert js["miss"] == "wd_999"
    assert js["nullMap"] == "wd_abc123"
    assert js["emptyMap"] == "wd_abc123"
    assert js["emptyName"] == "wd_abc123"


def test_synthetic_fold_rows_pass_through(js):
    # "Other (N projects)" / "unknown" are not project ids; they must
    # survive the join unchanged.
    assert js["otherRow"] == "Other (3 projects)"
    assert js["unknownRow"] == "unknown"


def test_dashboard_is_wired_to_the_projects_list():
    src = APP_JSX.read_text()
    # The chart rows are built through the helper on the join key...
    assert "projectDisplayLabel(r.project, nameByProject)" in src
    # ...keyed by the picker's project_id...
    assert "m[p.project_id] = p.display_name" in src
    # ...and Dashboard receives the projects list from App.
    assert "projects={projects}" in src
