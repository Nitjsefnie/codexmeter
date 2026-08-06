"""Codex rollouts, end to end: archiver key → R2 → ingest → rollups.

Everything the Kimi path already does, exercised over whole Codex rollout
files: the archiver derives each key, the objects land xz-compressed, r2 inflates
them transparently, ingest parses them, recompute_canonical collapses the
requests that several files replay, and the rollups sum what is left.

The mirror is built by the archiver's own key derivation rather than by a
hand-written layout, so a key convention that drifts from what ingest reads
fails HERE instead of silently mislabelling a live bucket.
"""
from __future__ import annotations

import importlib.util
import lzma
import os
import shutil
import tempfile
from pathlib import Path

import pytest

from backend import db, ingest

_REPO_ROOT = Path(__file__).resolve().parent.parent
FIX = _REPO_ROOT / "fixtures" / "codex"


def _load_archiver():
    """Import the archiver by path — scripts/ is not a package.

    `allow_module_level` is load-bearing: this runs at import time, where a
    plain pytest.skip() is a COLLECTION ERROR that aborts the whole session
    rather than skipping this module.
    """
    spec = importlib.util.spec_from_file_location(
        "archive_codex_sessions",
        _REPO_ROOT / "scripts" / "archive_codex_sessions.py",
    )
    assert spec is not None and spec.loader is not None, (
        "archiver module spec could not be built")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except ImportError as exc:  # pragma: no cover - environment-dependent
        pytest.skip(
            f"archiver dependencies unavailable: {exc}",
            allow_module_level=True,
        )
    return module


archiver = _load_archiver()

# The fixtures that carry a session_meta, so the archiver can key them.
# rollout_model_switch is a mid-file window with no head and is deliberately
# absent — it has no thread identity to file under.
KEYABLE = ("rollout_fork_prefix.jsonl", "rollout_sole_model_prefix.jsonl")


@pytest.fixture(name="fresh_db")
def _fresh_db(monkeypatch):
    test_db = "codexmeter_test"
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")
    os.system(f"createdb {test_db} 2>/dev/null")
    os.system(f"psql {test_db} -f {_REPO_ROOT / 'backend/schema.sql'} >/dev/null")
    monkeypatch.setenv("DATABASE_URL_VIZ", f"postgresql:///{test_db}")
    db.close_viz_pool()
    yield
    db.close_viz_pool()
    os.system(f"dropdb --if-exists {test_db} 2>/dev/null")


@pytest.fixture(name="codex_r2")
def _codex_r2(monkeypatch):
    """A file:// mirror built the way the archiver would build a bucket.

    Objects are written xz-compressed at <key>.xz, which is what the
    archiver does for text and what backend/r2.py inflates on read — so
    this fixture exercises the compression round trip, not just the layout.
    """
    tmp = tempfile.mkdtemp(prefix="cm-codex-ingest-")
    bucket = Path(tmp) / "r2" / "codex"
    written = {}
    for name in KEYABLE:
        path = FIX / name
        meta = archiver.read_session_meta(path)
        key = archiver.rollout_key(path, meta)
        assert key, f"fixture {name} produced no key"
        dest = bucket / (key + ".xz")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(lzma.compress(path.read_bytes()))
        written[name] = key
    monkeypatch.setenv("R2_ENDPOINT", f"file://{tmp}/r2/")
    monkeypatch.setenv("R2_BUCKET", "codex")
    yield written
    shutil.rmtree(tmp)


def _scalar(c, sql: str, args: tuple = ()):
    row = (c.execute(sql, args) if args else c.execute(sql)).fetchone()
    assert row is not None, f"query returned no rows: {sql[:60]}"
    return row[0]


def test_rollouts_ingest_from_an_xz_bucket(fresh_db, codex_r2):
    """The whole path: xz object → inflate → parse → rows."""
    result = ingest.run_ingest("test")
    assert result["error"] is None
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM files") == len(codex_r2)
        assert _scalar(c, "SELECT COUNT(*) FROM records") > 0
        assert _scalar(
            c, "SELECT COUNT(*) FROM records WHERE model LIKE 'gpt-5.6-%'"
        ) == _scalar(c, "SELECT COUNT(*) FROM records")


def test_the_key_tells_ingest_the_project_session_and_agent_split(
        fresh_db, codex_r2):
    """project_id, session_id and is_main are read out of the key, so this
    is the archiver's contract with ingest, checked from the far side."""
    ingest.run_ingest("test")
    with db.viz_conn() as c:
        # rollout_fork_prefix declares agent_path -> the subagents/ leg.
        fork_key = codex_r2["rollout_fork_prefix.jsonl"]
        is_main = _scalar(
            c, "SELECT is_main FROM files WHERE file_key = %s",
            (fork_key + ".xz",))
        assert is_main is False
        session_id = _scalar(
            c, "SELECT session_id FROM files WHERE file_key = %s",
            (fork_key + ".xz",))
        assert session_id == "00000000-0000-4000-8000-000000000001"
        # Its project is the cwd the rollout declared, hashed.
        assert _scalar(
            c, "SELECT COUNT(DISTINCT project_id) FROM files") >= 1


def test_replayed_requests_are_collapsed_by_recompute_canonical(
        fresh_db, codex_r2):
    """The same rollout filed under two keys stands in for the resume that
    replays a parent's history: both files parse, and exactly one copy of
    each request survives as canonical."""
    ingest.run_ingest("test")
    with db.viz_conn() as c:
        before = _scalar(c, "SELECT COUNT(*) FROM records")
        canonical = _scalar(
            c, "SELECT COUNT(*) FROM records WHERE is_canonical")
        distinct = _scalar(c, "SELECT COUNT(DISTINCT uuid) FROM records")
    assert canonical == distinct
    assert canonical <= before


def test_a_forks_records_dedup_against_the_file_that_replays_them(
        fresh_db, codex_r2, monkeypatch):
    """Two DIFFERENT keys holding the same thread's requests must leave one
    canonical row each — this is the 2.16x over-count the uuid prevents."""
    # File the same rollout a second time under a sibling subagent key, the
    # way a fork family lands in a real bucket.
    endpoint = os.environ["R2_ENDPOINT"]
    bucket = Path(endpoint[len("file://"):]) / "codex"
    src_key = codex_r2["rollout_fork_prefix.jsonl"]
    twin = src_key.replace("/subagents/", "/subagents/twin-")
    dest = bucket / (twin + ".xz")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes((bucket / (src_key + ".xz")).read_bytes())
    del monkeypatch

    ingest.run_ingest("test")
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM files") == len(codex_r2) + 1
        rows = _scalar(
            c, "SELECT COUNT(*) FROM records WHERE file_key IN (%s, %s)",
            (src_key + ".xz", twin + ".xz"))
        canon = _scalar(
            c, "SELECT COUNT(*) FROM records "
               "WHERE is_canonical AND file_key IN (%s, %s)",
            (src_key + ".xz", twin + ".xz"))
        # Both files contributed rows; only one copy of each request stands.
        assert rows == 2 * canon


def test_rollups_carry_every_token_type(fresh_db, codex_r2):
    """usage_rollup must total the Codex-only types, not just the ones the
    Kimi formats happen to have."""
    ingest.run_ingest("test")
    with db.viz_conn() as c:
        assert _scalar(c, "SELECT COUNT(*) FROM usage_rollup") > 0
        # Reasoning tokens are real in this corpus.
        assert _scalar(
            c, "SELECT SUM(reasoning_output_tokens) FROM usage_rollup") > 0
        # Cache writes are zero in it — stored and summed all the same, so
        # the day one lands it needs no migration.
        assert _scalar(
            c, "SELECT SUM(cache_write_tokens) FROM usage_rollup") == 0
        # The rollup sums only canonical rows.
        assert _scalar(c, "SELECT SUM(requests) FROM usage_rollup") == _scalar(
            c, "SELECT COUNT(*) FROM records "
               "WHERE is_canonical AND ts IS NOT NULL")


def test_rollup_reasoning_matches_the_records_it_sums(fresh_db, codex_r2):
    ingest.run_ingest("test")
    with db.viz_conn() as c:
        rolled = _scalar(
            c, "SELECT SUM(reasoning_output_tokens) FROM usage_rollup")
        raw = _scalar(
            c, "SELECT SUM(reasoning_output_tokens) FROM records "
               "WHERE is_canonical AND ts IS NOT NULL")
    assert rolled == raw
