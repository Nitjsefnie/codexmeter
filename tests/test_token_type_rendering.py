"""Token types render by what the data actually contains.

The rule, in one line: every token type any format reports is parsed,
stored, summed and priced — and a type whose total is zero for the data in
view is hidden from the RESPONSE, not dropped from the pipeline.

That distinction is the whole point, so both halves are tested:

  * hidden — the shipped fixture corpus is Kimi, which reports neither
    reasoning nor cache-write tokens, so neither key reaches the wire;
  * shown — constructed deliberately, because no file in the current
    corpus exercises it. This is the direction that would rot silently:
    the day a session lands with real cache writes it must appear on its
    own, with no migration and no code change, and nothing else here
    proves that.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend import api_dashboard
from backend.api_dashboard import (
    TOKEN_TYPE_FIELDS,
    _drop_zero_token_types,
    _token_type_metadata,
)

# Reuses the API suite's fresh-DB + mini-R2 fixture (Kimi transcripts).
pytest_plugins = ["test_api"]


def _entry(**tokens) -> dict:
    """One hourly entry carrying every token type, zero unless named."""
    entry = {"hour": "2026-08-05T00:00:00Z", "model": "kimi-k2-7-code",
             "cost_usd": 1.0, "requests": 1, "session_count": 1}
    entry.update(dict.fromkeys(TOKEN_TYPE_FIELDS, 0))
    entry.update(tokens)
    return entry


# --------------------------------------------------------------------------
# Hidden: the total is zero across the response
# --------------------------------------------------------------------------


def test_a_token_type_that_is_zero_everywhere_is_dropped():
    out = _drop_zero_token_types([
        _entry(input_tokens=100, output_tokens=10),
        _entry(input_tokens=200, output_tokens=20),
    ])
    for entry in out:
        assert "cache_write_tokens" not in entry
        assert "reasoning_output_tokens" not in entry
        assert "cache_read_tokens" not in entry
        # Non-token keys are never touched.
        assert entry["cost_usd"] == 1.0
        assert entry["model"] == "kimi-k2-7-code"


def test_suppression_is_decided_per_response_not_per_entry():
    """A type present in ONE bucket stays in every bucket, including the
    ones where it is zero — otherwise a series would flicker in and out
    between buckets and a chart could not plot it."""
    out = _drop_zero_token_types([
        _entry(input_tokens=100, cache_write_tokens=0),
        _entry(input_tokens=100, cache_write_tokens=7),
    ])
    assert [e["cache_write_tokens"] for e in out] == [0, 7]


def test_an_empty_response_drops_every_token_type():
    assert not _drop_zero_token_types([])


def test_nothing_is_dropped_when_every_type_has_data():
    entries = [_entry(**dict.fromkeys(TOKEN_TYPE_FIELDS, 5))]
    out = _drop_zero_token_types(entries)
    for field in TOKEN_TYPE_FIELDS:
        assert out[0][field] == 5


@pytest.mark.parametrize("field", TOKEN_TYPE_FIELDS)
def test_the_rule_is_general_across_every_token_type(field):
    """No token type is special-cased — suppression is a property of being
    a token type. Each one hides when alone-zero and shows when alone-set.
    """
    others = {f: 3 for f in TOKEN_TYPE_FIELDS if f != field}

    hidden = _drop_zero_token_types([_entry(**others)])[0]
    assert field not in hidden

    shown = _drop_zero_token_types([_entry(**others, **{field: 1})])[0]
    assert shown[field] == 1


# --------------------------------------------------------------------------
# Shown: constructed deliberately — no corpus file reaches this yet
# --------------------------------------------------------------------------


def test_cache_write_tokens_reach_the_wire_once_a_session_reports_them():
    """The day a Codex session bills real cache writes, the type appears
    with no migration, no backfill and no change here. Constructed, because
    every rollout on the box this was built from reports zero."""
    out = _drop_zero_token_types([
        _entry(input_tokens=1000, output_tokens=50, cache_write_tokens=880),
    ])
    assert out[0]["cache_write_tokens"] == 880
    assert "reasoning_output_tokens" not in out[0]


def test_reasoning_tokens_reach_the_wire_when_a_format_reports_them():
    out = _drop_zero_token_types([
        _entry(input_tokens=1000, output_tokens=50,
               reasoning_output_tokens=31),
    ])
    assert out[0]["reasoning_output_tokens"] == 31
    assert "cache_write_tokens" not in out[0]


def test_a_type_is_kept_on_a_single_nonzero_bucket_among_many_zeros():
    """The realistic shape of a corpus growing into a new token type: one
    session uses it, the rest of the range does not."""
    entries = [_entry(input_tokens=10) for _ in range(20)]
    entries[7]["cache_write_tokens"] = 1
    out = _drop_zero_token_types(entries)
    assert all("cache_write_tokens" in e for e in out)
    assert sum(e["cache_write_tokens"] for e in out) == 1


def test_metadata_distinguishes_total_addends_from_output_subsets():
    hourly = _drop_zero_token_types([_entry(
        input_tokens=100, output_tokens=50, cache_write_tokens=3,
        reasoning_output_tokens=7,
    )])
    metadata = _token_type_metadata(hourly)
    by_field = {item["field"]: item for item in metadata}

    assert set(by_field) == {
        "input_tokens", "output_tokens", "cache_write_tokens",
        "reasoning_output_tokens",
    }
    assert by_field["cache_write_tokens"] == {
        "field": "cache_write_tokens", "label": "Cache Write",
        "total": True, "rate": "create",
    }
    assert by_field["reasoning_output_tokens"] == {
        "field": "reasoning_output_tokens", "label": "Reasoning Output",
        "total": False, "rate": None,
    }


def test_metadata_omits_every_zero_suppressed_type():
    hourly = _drop_zero_token_types([
        _entry(input_tokens=100, output_tokens=50),
    ])
    assert [item["field"] for item in _token_type_metadata(hourly)] == [
        "input_tokens", "output_tokens",
    ]


# --------------------------------------------------------------------------
# End to end, against the real dashboard payload
# --------------------------------------------------------------------------


def test_the_dashboard_hides_types_the_kimi_fixture_corpus_never_reports(
        app_with_data):
    """Kimi transcripts carry no reasoning breakdown and bill cache
    creation at zero, so neither key belongs on this wire."""
    body = app_with_data.get("/api/dashboard?range=all").json()
    assert body["hourly"], "fixture corpus produced no hourly buckets"
    for entry in body["hourly"]:
        assert "reasoning_output_tokens" not in entry
        assert "cache_write_tokens" not in entry


def test_the_dashboard_still_sends_the_types_that_do_have_data(
        app_with_data):
    """Suppression must not take the live types with it."""
    body = app_with_data.get("/api/dashboard?range=all").json()
    totals = {
        field: sum(e.get(field, 0) for e in body["hourly"])
        for field in TOKEN_TYPE_FIELDS
    }
    assert totals["input_tokens"] > 0
    assert totals["output_tokens"] > 0
    for entry in body["hourly"]:
        assert "input_tokens" in entry
        assert "output_tokens" in entry
    assert [item["field"] for item in body["token_types"]] == [
        field for field in TOKEN_TYPE_FIELDS if totals[field] > 0
    ]


def test_every_token_type_field_is_one_the_rollup_can_actually_supply():
    """TOKEN_TYPE_FIELDS drives suppression, so a name here that no query
    selects would be permanently 'zero' and permanently hidden."""
    text = Path(api_dashboard.__file__).read_text(encoding="utf-8")
    for field in TOKEN_TYPE_FIELDS:
        assert f"AS {field}" in text or f'"{field}"' in text, field
