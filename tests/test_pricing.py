"""MODEL_RATES is the single source of truth for cost in this repo.
Mirrors backend/pricing.py — bump PARSER_VERSION whenever the table changes.
"""
from datetime import datetime, timedelta, timezone

import pytest

from backend import pricing

UTC = timezone.utc


def test_k2_7_code_rates_match_source():
    r = pricing.rate_for("kimi-k2-7-code")
    assert r == {"fresh": 0.95, "create": 0.00, "read": 0.19, "output": 4.00}


def test_k2_6_rates_match_source():
    r = pricing.rate_for("kimi-k2-6")
    assert r == {"fresh": 0.95, "create": 0.00, "read": 0.16, "output": 4.00}


def test_k3_rates_match_source():
    r = pricing.rate_for("kimi-k3")
    assert r == {"fresh": 3.00, "create": 0.00, "read": 0.30, "output": 15.00}


@pytest.mark.parametrize(("model", "want"), [
    ("gpt-6-astra", {"fresh": 10.00, "create": 12.50,
                     "read": 1.00, "output": 50.00}),
    ("gpt-5.6-sol", {"fresh": 4.00, "create": 5.00,
                     "read": 0.40, "output": 20.00}),
    ("gpt-5.6-terra", {"fresh": 2.00, "create": 2.50,
                       "read": 0.20, "output": 12.00}),
    ("gpt-5.6-luna", {"fresh": 0.20, "create": 0.25,
                      "read": 0.02, "output": 1.20}),
])
def test_codex_rates_match_source(model, want):
    """A Codex label must reach the same table the browser mirrors.

    These are the CURRENT list rates. Everything the family charged before
    them lives in DATED_RATES; see test_codex_dated_rates_match_source.
    """
    assert pricing.MODEL_RATES[model] == want
    assert pricing.rate_for(model) is pricing.MODEL_RATES[model]


# The GPT-5.6 family repriced twice, and each cut moved a different subset:
#   2026-07-09  GA          sol 5/30    terra 2.50/15   luna 1/6
#   2026-07-30  luna -80%, terra -20%   sol untouched
#   2026-08-21  sol -20% in / -33% out  terra and luna untouched
# Cached reads are 10% of uncached input and cache writes 1.25x it, in every
# window of every tier — verified against OpenAI's own Sol table, which
# prints 4 / 0.4 / 5 / 20.
BEFORE_JUL30 = datetime(2026, 7, 20, tzinfo=UTC)
BETWEEN_CUTS = datetime(2026, 8, 10, tzinfo=UTC)
AFTER_AUG21 = datetime(2026, 8, 24, tzinfo=UTC)


@pytest.mark.parametrize(("model", "ts", "want"), [
    ("gpt-5.6-sol", BEFORE_JUL30, {"fresh": 5.00, "create": 6.25,
                                   "read": 0.50, "output": 30.00}),
    ("gpt-5.6-sol", BETWEEN_CUTS, {"fresh": 5.00, "create": 6.25,
                                   "read": 0.50, "output": 30.00}),
    ("gpt-5.6-sol", AFTER_AUG21, {"fresh": 4.00, "create": 5.00,
                                  "read": 0.40, "output": 20.00}),
    ("gpt-5.6-terra", BEFORE_JUL30, {"fresh": 2.50, "create": 3.125,
                                     "read": 0.25, "output": 15.00}),
    ("gpt-5.6-terra", BETWEEN_CUTS, {"fresh": 2.00, "create": 2.50,
                                     "read": 0.20, "output": 12.00}),
    ("gpt-5.6-terra", AFTER_AUG21, {"fresh": 2.00, "create": 2.50,
                                    "read": 0.20, "output": 12.00}),
    ("gpt-5.6-luna", BEFORE_JUL30, {"fresh": 1.00, "create": 1.25,
                                    "read": 0.10, "output": 6.00}),
    ("gpt-5.6-luna", BETWEEN_CUTS, {"fresh": 0.20, "create": 0.25,
                                    "read": 0.02, "output": 1.20}),
    ("gpt-5.6-luna", AFTER_AUG21, {"fresh": 0.20, "create": 0.25,
                                   "read": 0.02, "output": 1.20}),
])
def test_codex_dated_rates_match_source(model, ts, want):
    assert pricing.rate_for(model, ts) == want


@pytest.mark.parametrize(("model", "epoch"), [
    ("gpt-5.6-terra", pricing.JUL30_CUT),
    ("gpt-5.6-luna", pricing.JUL30_CUT),
    ("gpt-5.6-sol", pricing.AUG21_CUT),
])
def test_a_cut_instant_belongs_to_the_new_price(model, epoch):
    """Boundaries are strictly-before / inclusive-at, matching the cutoff
    convention parse.py already uses for the Kimi generations. One
    microsecond earlier is still the old price.
    """
    assert pricing.rate_for(model, epoch) is pricing.MODEL_RATES[model]
    before = epoch - timedelta(microseconds=1)
    assert pricing.rate_for(model, before) is not pricing.MODEL_RATES[model]


def test_omitting_the_timestamp_yields_current_list_price():
    """Every caller that has no timestamp — an API filter, a rate display —
    must get today's price, never a retired one.
    """
    for model in ("gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        assert pricing.rate_for(model) is pricing.MODEL_RATES[model]
        assert pricing.rate_for(model, None) is pricing.MODEL_RATES[model]


def test_a_naive_timestamp_is_read_as_utc():
    """psycopg can hand back a naive datetime; treating it as local time
    would move the boundary by the server's offset.
    """
    naive = pricing.AUG21_CUT.replace(tzinfo=None) - timedelta(hours=1)
    assert pricing.rate_for("gpt-5.6-sol", naive)["output"] == 30.00


def test_rate_epochs_are_sorted_and_cover_every_window():
    assert pricing.RATE_EPOCHS == sorted(pricing.RATE_EPOCHS)
    assert set(pricing.RATE_EPOCHS) == {
        end for windows in pricing.DATED_RATES.values() for end, _ in windows
    }


def test_a_dated_window_never_leaks_onto_an_unknown_model():
    """DATED_RATES is keyed by exact table key. An id that only reaches the
    flagged default must get today's default, not some retired window of
    whatever model the fallback happens to point at.
    """
    for ts in (BEFORE_JUL30, BETWEEN_CUTS, AFTER_AUG21):
        assert pricing.rate_for("not-a-real-model", ts) is pricing.DEFAULT_RATES


def test_a_dated_lookup_still_reports_the_match_as_exact():
    r = pricing.resolve("gpt-5.6-luna", BEFORE_JUL30)
    assert r.kind == "exact"
    assert r.key == "gpt-5.6-luna"
    assert r.estimated is False


def test_compute_cost_bills_a_record_at_the_rate_of_its_own_timestamp():
    old = pricing.compute_cost(
        "gpt-5.6-luna", fresh=1_000_000, create=0, read=0, output=0,
        ts=BEFORE_JUL30,
    )
    new = pricing.compute_cost(
        "gpt-5.6-luna", fresh=1_000_000, create=0, read=0, output=0,
        ts=BETWEEN_CUTS,
    )
    assert old == pytest.approx(1.00, rel=1e-9)
    assert new == pytest.approx(0.20, rel=1e-9)


def test_luna_is_the_flagged_unknown_model_fallback():
    """The fallback must be a model codexmeter can actually emit, while
    remaining the cheapest choice for a value explicitly marked estimated.
    """
    assert pricing.DEFAULT_RATES is pricing.MODEL_RATES["gpt-5.6-luna"]


def test_k3_does_not_fall_through_to_default_rates():
    """kimi-k3 shares no substring with the k2 keys, so a missing table entry
    would silently bill it at the cheaper generic fallback.
    """
    assert pricing.rate_for("kimi-k3") is not pricing.DEFAULT_RATES


def test_k3_is_pricier_than_k2_on_every_billed_bucket():
    k3 = pricing.MODEL_RATES["kimi-k3"]
    k2 = pricing.MODEL_RATES["kimi-k2-7-code"]
    for bucket in ("fresh", "read", "output"):
        assert k3[bucket] > k2[bucket]


def test_unknown_model_falls_back_to_default():
    r = pricing.rate_for("not-a-real-model")
    assert r == pricing.DEFAULT_RATES


# Every label either parser can emit.
LIVE_MODELS = (
    "kimi-k3", "kimi-k2-7-code", "kimi-k2-6",
    "gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
)


@pytest.mark.parametrize("model", LIVE_MODELS)
def test_live_model_ids_resolve_exact(model):
    r = pricing.resolve(model)
    assert r.kind == "exact"
    assert r.key == model
    assert r.estimated is False
    assert r.rates is pricing.MODEL_RATES[model]


def test_every_table_key_resolves_to_itself():
    """A key whose own shape the matcher rejects would price nothing."""
    for key in pricing.MODEL_RATES:
        assert pricing.resolve(key).key == key


@pytest.mark.parametrize("model", [
    "kimi-k2-6-turbo",
    "kimi-k2-7-code-fast",
    "kimi-k3-mini",
    "kimi-k2-60",
    "x-kimi-k2-7-code-y",
    "kimi-code/k3",
])
def test_an_id_merely_containing_a_table_key_is_not_an_exact_match(model):
    """Anchored matching, not substring. A model the table has never heard
    of must not inherit the rates of whatever key sits inside its name.
    """
    r = pricing.resolve(model)
    assert r.kind == "default"
    assert r.key is None
    assert r.estimated is True


def test_an_unknown_k3_variant_is_not_billed_at_k3_rates():
    """The substring matcher priced "kimi-k3-mini" at K3's ~3x rates on the
    strength of a name. Unknown means unknown: default rates, flagged.
    """
    assert pricing.rate_for("kimi-k3-mini") != pricing.MODEL_RATES["kimi-k3"]
    assert pricing.rate_for("kimi-k3-mini") is pricing.DEFAULT_RATES


@pytest.mark.parametrize("model", [
    "kimi-k3-20260601",   # dated snapshot of the same model
    "kimi-k3[1m]",        # context-window bracket suffix
    "kimi-k3@latest",     # provider alias suffix
    "KIMI-K3",            # case
    "  kimi-k3  ",        # padding
    "kimi-k2.7-code",     # dotted version separator
])
def test_suffixes_that_still_name_a_known_model_resolve_exact(model):
    assert pricing.resolve(model).kind == "exact"


def test_unknown_model_is_flagged_estimated():
    r = pricing.resolve("not-a-real-model")
    assert r.kind == "default"
    assert r.estimated is True
    assert r.rates is pricing.DEFAULT_RATES


def test_empty_model_resolves_to_the_flagged_default():
    r = pricing.resolve("")
    assert r.kind == "default"
    assert r.key is None
    assert r.estimated is True
    assert r.rates is pricing.DEFAULT_RATES


@pytest.mark.parametrize("model", LIVE_MODELS)
def test_rate_for_still_returns_the_table_dict_for_live_models(model):
    assert pricing.rate_for(model) is pricing.MODEL_RATES[model]


def test_rate_for_still_defaults_for_empty_missing_and_unknown_ids():
    # api._per_model passes a model straight off a DB row, which may be NULL.
    assert pricing.rate_for("") is pricing.DEFAULT_RATES
    assert pricing.rate_for(None) is pricing.DEFAULT_RATES
    assert pricing.rate_for("not-a-real-model") is pricing.DEFAULT_RATES


def test_k2_7_code_compute_cost_known_vector():
    cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=1_000_000, create=0, read=0, output=0,
    )
    assert cost == pytest.approx(0.95, rel=1e-9)


def test_k2_6_read_rate_is_cheaper_than_k2_7_code():
    cost_7 = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=0, create=0, read=1_000_000, output=0,
    )
    cost_6 = pricing.compute_cost(
        "kimi-k2-6",
        fresh=0, create=0, read=1_000_000, output=0,
    )
    assert cost_7 == pytest.approx(0.19, rel=1e-9)
    assert cost_6 == pytest.approx(0.16, rel=1e-9)
    assert cost_6 < cost_7


def test_cache_creation_is_free_for_kimi_models():
    cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=0, create=1_000_000, read=0, output=0,
    )
    assert cost == pytest.approx(0.00, rel=1e-9)


def test_mixed_usage_cost_is_sum_of_buckets():
    cost = pricing.compute_cost(
        "kimi-k2-7-code",
        fresh=100_000, create=50_000, read=200_000, output=10_000,
    )
    expected = (
        100_000 * 0.95 / 1_000_000
        + 50_000 * 0.00 / 1_000_000
        + 200_000 * 0.19 / 1_000_000
        + 10_000 * 4.00 / 1_000_000
    )
    assert cost == pytest.approx(expected, rel=1e-9)


def test_astra_carries_no_dated_window():
    """Astra shipped after the GPT-5.6 repricings and has held one price.

    A window inherited from a neighbouring key would bill every Astra
    record before that boundary at rates Astra never charged, so the
    absence is asserted rather than left to the table's shape.
    """
    assert "gpt-6-astra" not in pricing.DATED_RATES
    for epoch in pricing.RATE_EPOCHS:
        before = epoch - timedelta(seconds=1)
        assert pricing.rate_for("gpt-6-astra", before) is (
            pricing.MODEL_RATES["gpt-6-astra"])


def test_astra_is_the_priciest_current_codex_model():
    """The Codex fallback bills unknown ids at the flagship rate, and
    parse_codex names Astra as that flagship. If some other current label
    ever outprices it, the fallback stops erring upward and starts
    understating — silently, which is the failure mode it exists to avoid.
    """
    astra = pricing.MODEL_RATES["gpt-6-astra"]
    others = [rates for model, rates in pricing.MODEL_RATES.items()
              if model.startswith("gpt-") and model != "gpt-6-astra"]
    for rates in others:
        for bucket in ("fresh", "create", "read", "output"):
            assert astra[bucket] > rates[bucket]
