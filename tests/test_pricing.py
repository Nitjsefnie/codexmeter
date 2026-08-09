"""MODEL_RATES is the single source of truth for cost in this repo.
Mirrors backend/pricing.py — bump PARSER_VERSION whenever the table changes.
"""
import pytest

from backend import pricing


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
    ("gpt-5.6-sol", {"fresh": 5.00, "create": 6.25,
                     "read": 0.50, "output": 30.00}),
    ("gpt-5.6-terra", {"fresh": 2.00, "create": 2.50,
                       "read": 0.20, "output": 12.00}),
    ("gpt-5.6-luna", {"fresh": 0.20, "create": 0.25,
                      "read": 0.02, "output": 1.20}),
])
def test_codex_rates_match_source(model, want):
    """A Codex label must reach the same table the browser mirrors."""
    assert pricing.MODEL_RATES[model] == want
    assert pricing.rate_for(model) is pricing.MODEL_RATES[model]


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
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna",
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
