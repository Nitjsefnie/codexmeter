"""The Codex archiver's key derivation.

Only the pure half is tested here — deriving an R2 key from a rollout. The
upload half needs credentials and a bucket, and is a faithful copy of the
two sibling archivers' logic rather than anything new.

Why this matters more than it looks: backend/ingest.py reads project_id,
session_id and is_main STRAIGHT OUT OF THE KEY, before it fetches anything.
A key that is wrong is not a cosmetic problem — it mislabels every record
in the file for as long as the object exists.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = _REPO_ROOT / "fixtures" / "codex"


def _load_archiver():
    """Import the archiver by path — scripts/ is not a package.

    Skips rather than fails when a third-party dependency of the archiver
    is absent: its absence says nothing about this repo's code. The
    `allow_module_level` flag is load-bearing — this runs at import time,
    and a plain pytest.skip() there is a COLLECTION ERROR that aborts the
    entire session, not a skip of this module.
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


# --------------------------------------------------------------------------
# Project bucketing
# --------------------------------------------------------------------------


PROJECT = "/workspace/toy-project"
OTHER_PROJECT = "/workspace/other-project"


def test_the_same_project_always_lands_in_the_same_bucket():
    assert archiver.project_hash(PROJECT) == archiver.project_hash(PROJECT)


def test_different_projects_land_in_different_buckets():
    assert archiver.project_hash(PROJECT) != archiver.project_hash(
        OTHER_PROJECT)


def test_a_project_hash_is_twelve_hex_characters():
    """The shape ingest expects in the <hash> position of the key."""
    value = archiver.project_hash(OTHER_PROJECT)
    assert len(value) == 12
    assert all(ch in "0123456789abcdef" for ch in value)


# --------------------------------------------------------------------------
# Rollout uuid
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name,expected", [
    ("rollout-2026-06-14T12-00-00-00000000-0000-4000-8000-000000000002.jsonl",
     "00000000-0000-4000-8000-000000000002"),
    ("rollout-2026-06-14T21-13-09-11111111-2222-4333-8444-555555555555.jsonl",
     "11111111-2222-4333-8444-555555555555"),
])
def test_the_rollout_uuid_is_read_off_the_filename(name, expected):
    """The timestamp in the name is hyphenated too, so a naive split on '-'
    picks up the clock, not the uuid."""
    assert archiver.rollout_uuid(name) == expected


# --------------------------------------------------------------------------
# Key derivation
# --------------------------------------------------------------------------


THREAD_ID = "00000000-0000-4000-8000-000000000001"
FILE_ID = "00000000-0000-4000-8000-000000000002"
PARENT_ID = "00000000-0000-4000-8000-000000000003"


def _meta(**over):
    meta = {"session_id": THREAD_ID, "cwd": PROJECT}
    meta.update(over)
    return meta


NAME = f"rollout-2026-06-14T12-00-00-{FILE_ID}.jsonl"


def test_a_main_rollout_is_keyed_by_its_thread_id():
    """A main thread has exactly one rollout file — measured: 9 files, 9
    thread ids — so the thread id alone is a unique key for it."""
    key = archiver.rollout_key(NAME, _meta())
    assert key == (
        f"sessions/{archiver.project_hash(PROJECT)}/{THREAD_ID}/wire.jsonl")


@pytest.mark.parametrize("marker", [
    {"agent_path": f"{PROJECT}/agents/doc-check"},
    {"parent_thread_id": PARENT_ID},
])
def test_a_subagent_rollout_is_keyed_per_file_under_subagents(marker):
    """A subagent thread REUSES its parent's session_id — measured: 42
    files across 2 thread ids — so keying it by thread id alone would make
    41 of them overwrite each other."""
    key = archiver.rollout_key(NAME, _meta(**marker))
    assert key.endswith(f"/subagents/{FILE_ID}/wire.jsonl")
    assert f"/{THREAD_ID}/subagents/" in key


def test_the_subagent_leg_is_what_gives_ingest_its_is_main_split():
    """ingest classifies on the literal '/subagents/' segment."""
    main = archiver.rollout_key(NAME, _meta())
    sub = archiver.rollout_key(NAME, _meta(agent_path=f"{PROJECT}/a"))
    assert "/subagents/" not in main
    assert "/subagents/" in sub


@pytest.mark.parametrize("meta", [
    None,
    {},
    {"cwd": PROJECT},                     # no thread id
    {"session_id": THREAD_ID},            # no project
])
def test_a_rollout_that_cannot_be_placed_is_skipped_not_guessed(meta):
    """ingest reads project and session out of the key, so a guess here is
    wrong for the life of the object."""
    assert archiver.rollout_key(NAME, meta) is None


def test_every_key_parses_the_way_ingest_reads_it():
    """The contract that actually matters: ingest splits on '/' and takes
    parts[1] as project_id and parts[2] as session_id, requiring at least
    4 parts and a wire.jsonl leaf."""
    for meta in (_meta(), _meta(agent_path=f"{PROJECT}/agents/doc-check")):
        key = archiver.rollout_key(NAME, meta)
        parts = key.split("/")
        assert parts[0] == "sessions"
        assert len(parts) >= 4
        assert parts[-1] == "wire.jsonl"
        assert parts[1] == archiver.project_hash(PROJECT)
        assert parts[2] == THREAD_ID


# --------------------------------------------------------------------------
# Reading the head of a rollout
# --------------------------------------------------------------------------


def test_session_meta_is_read_from_a_rollout_head():
    meta = archiver.read_session_meta(FIX / "rollout_fork_prefix.jsonl")
    assert meta is not None
    assert meta["session_id"] == THREAD_ID
    assert meta["agent_path"] == "/workspace/toy-project/agents/doc-check"
    assert meta["cwd"]


def test_a_fork_rollout_keys_onto_the_subagent_leg():
    """End to end over a whole file: this fixture declares agent_path, so
    it must not take the main leg and collide with its parent."""
    path = FIX / "rollout_fork_prefix.jsonl"
    key = archiver.rollout_key(path, archiver.read_session_meta(path))
    assert "/subagents/" in key
    assert key.endswith("/wire.jsonl")


def test_a_windowed_fragment_has_no_session_meta_and_is_skipped():
    path = FIX / "rollout_model_switch.jsonl"
    assert archiver.read_session_meta(path) is None
    assert archiver.rollout_key(path, None) is None


def test_the_head_scan_stops_early_on_a_file_with_no_meta(tmp_path):
    """A 20MB rollout must not be read end to end to learn it has no head."""
    big = tmp_path / "rollout-2026-08-05T00-00-00-a-b-c-d-e.jsonl"
    big.write_text("\n".join(
        json.dumps({"type": "event_msg", "payload": {"type": "token_count"}})
        for _ in range(5000)
    ), encoding="utf-8")
    assert archiver.read_session_meta(big) is None


def test_unreadable_paths_report_no_meta_rather_than_raising(tmp_path):
    assert archiver.read_session_meta(tmp_path / "nope.jsonl") is None


# --------------------------------------------------------------------------
# Compression policy
# --------------------------------------------------------------------------


def test_rollouts_are_stored_compressed():
    """Every key this archiver writes ends .jsonl, and text is always
    stored xz — which is what backend/r2.py transparently inflates.

    Reaching for the private predicate on purpose: the compression choice
    has no public surface, and the alternative is a live R2 client.
    """
    # pylint: disable=protected-access
    assert archiver._is_text("sessions/abc/def/wire.jsonl")
    assert not archiver._is_text("sessions/abc/def/blob.bin")
