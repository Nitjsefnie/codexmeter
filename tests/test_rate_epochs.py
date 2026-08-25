"""backend/api_rates.py — the dated-rate read helpers.

cost_total always comes from SUM(cost_usd), the per-record cost computed at
ingest against that record's own timestamp, and is right by construction.
cost_buckets is re-derived from SUMMED tokens, so it is only right if the
sum is taken per rate epoch. These drive the fold directly: no DB, because
the arithmetic is what can be wrong, not the query.
"""
import pytest

from backend import api_rates, pricing


# --------------------------------------------------------------------------
# Dated rates: cost_buckets are re-derived from summed tokens, so they have
# to be summed per rate epoch or they stop adding up to the cost_total they
# decompose. No DB — these drive the fold directly.
# --------------------------------------------------------------------------


def test_rate_epoch_sql_binds_one_case_arm_per_boundary():
    expr, params = api_rates.rate_epoch_sql("ts")
    assert params == list(pricing.RATE_EPOCHS)
    assert expr.count("CASE WHEN ts >= %s") == len(pricing.RATE_EPOCHS)
    assert expr.count("%s") == len(params)


def test_epoch_ts_lands_inside_the_epoch_it_names():
    epochs = pricing.RATE_EPOCHS
    assert epochs, "this test is meaningless with no dated rates"
    zeroth = api_rates.epoch_ts(0)
    assert zeroth is not None and zeroth < epochs[0]
    for i, boundary in enumerate(epochs, start=1):
        assert api_rates.epoch_ts(i) == boundary
    # Above the last index, clamp rather than index out of range.
    assert api_rates.epoch_ts(len(epochs) + 5) == epochs[-1]


def test_cost_buckets_are_priced_per_epoch_and_still_sum_to_the_total():
    """Sol went 5/30 -> 4/20 mid-corpus. A range spanning that instant must
    price each half at its own rates: flattening to today's table makes the
    breakdown disagree with the SUM(cost_usd) printed beside it.
    """
    # (model, rate_epoch, turns, fresh, cache_read, output, cost)
    rows = [
        ("gpt-5.6-sol", 1, 10, 1_000_000, 0, 100_000, 5.0 + 3.0),
        ("gpt-5.6-sol", 2, 5, 1_000_000, 0, 100_000, 4.0 + 2.0),
    ]
    folded = api_rates.fold_per_model(rows)

    assert len(folded) == 1
    entry = folded[0]
    assert entry["turns"] == 15
    assert entry["fresh"] == 2_000_000
    assert entry["output"] == 200_000
    assert entry["cost_total"] == pytest.approx(14.0)
    assert entry["cost_buckets"]["fresh"] == pytest.approx(9.0)   # 5.00 + 4.00
    assert entry["cost_buckets"]["output"] == pytest.approx(5.0)  # 3.00 + 2.00
    assert sum(entry["cost_buckets"].values()) == pytest.approx(
        entry["cost_total"])
    # The bug this guards: one flat lookup at today's rates yields 12.0.
    flat = pricing.rate_for("gpt-5.6-sol")
    assert (2_000_000 * flat["fresh"] + 200_000 * flat["output"]) / 1_000_000 \
        != pytest.approx(entry["cost_total"])


def test_fold_keeps_one_entry_per_model_across_epochs():
    """The epoch index is shared, but which window it selects is per model:
    epoch 1 is between the two cuts, so Luna is already repriced there
    while Sol is not. Only epoch 0 predates Luna's cut.
    """
    rows = [
        ("gpt-5.6-luna", 0, 1, 1_000_000, 0, 0, 1.0),
        ("gpt-5.6-sol", 2, 1, 1_000_000, 0, 0, 4.0),
        ("gpt-5.6-luna", 2, 1, 1_000_000, 0, 0, 0.2),
    ]
    folded = api_rates.fold_per_model(rows)
    assert [e["model"] for e in folded] == ["gpt-5.6-sol", "gpt-5.6-luna"]
    luna = next(e for e in folded if e["model"] == "gpt-5.6-luna")
    assert luna["cost_total"] == pytest.approx(1.2)
    assert luna["cost_buckets"]["fresh"] == pytest.approx(1.2)
