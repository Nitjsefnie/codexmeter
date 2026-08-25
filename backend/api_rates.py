"""Dated-rate read helpers, shared by every endpoint that re-derives cost.

Split out of api.py, which is at its module-length limit, and named after
claudit's api_common.py so the two repos' copies of this machinery stay
findable from each other.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from backend import pricing


# --- dated-rate helpers ---------------------------------------------------
# cost_total always comes from SUM(cost_usd) — the per-record cost computed
# at ingest against that record's own timestamp. cost_buckets, by contrast,
# is re-derived from summed tokens, so it has to be aggregated per rate
# epoch or it silently disagrees with the total it claims to decompose the
# moment a range straddles a rate change. See backend/pricing.RATE_EPOCHS.


def rate_epoch_sql(ts_column: str) -> tuple[str, list]:
    """SQL expression yielding a 0-based rate-epoch index, plus its params.

    Index 0 is everything before the first boundary; each boundary the row
    is at-or-after adds one. Boundaries bind as parameters, never as
    interpolated literals.
    """
    cases = [
        f"(CASE WHEN {ts_column} >= %s THEN 1 ELSE 0 END)"
        for _ in pricing.RATE_EPOCHS
    ]
    expr = " + ".join(["0", *cases]) if cases else "0"
    return expr, list(pricing.RATE_EPOCHS)


def epoch_ts(index: int) -> datetime | None:
    """A timestamp lying inside rate epoch `index`, for rate lookup.

    Boundaries are inclusive-at, so a non-zero epoch is represented by its
    own opening instant, and epoch 0 by one microsecond before the first.
    """
    if not pricing.RATE_EPOCHS:
        return None
    if index <= 0:
        return pricing.RATE_EPOCHS[0] - timedelta(microseconds=1)
    return pricing.RATE_EPOCHS[min(index, len(pricing.RATE_EPOCHS)) - 1]


def _empty_model_entry(model: str) -> dict:
    return {
        "model": model,
        "turns": 0,
        "fresh": 0,
        "cache_read": 0,
        "output": 0,
        "estimated_rate": pricing.resolve(model).estimated,
        "_buckets": {"fresh": 0.0, "read": 0.0, "output": 0.0},
        "cost_total": 0.0,
    }


def _accumulate_model_row(acc: dict[str, dict], row) -> None:
    """Fold one (model, rate_epoch, ...) aggregate row into `acc`."""
    model, epoch, turns, fresh, cr, output, cost = row
    fresh = int(fresh or 0)
    cr = int(cr or 0)
    output = int(output or 0)
    rates = pricing.rate_for(model, epoch_ts(int(epoch or 0)))

    entry = acc.setdefault(model, _empty_model_entry(model))
    entry["turns"] += int(turns or 0)
    entry["fresh"] += fresh
    entry["cache_read"] += cr
    entry["output"] += output
    entry["cost_total"] += float(cost or 0)
    buckets = entry["_buckets"]
    buckets["fresh"] += fresh * rates["fresh"] / 1_000_000
    buckets["read"] += cr * rates["read"] / 1_000_000
    buckets["output"] += output * rates["output"] / 1_000_000


def fold_per_model(rows) -> list[dict]:
    """Fold (model, rate_epoch, ...) aggregate rows into one entry per model.

    Token counts and cost_total sum straight across epochs; cost_buckets are
    priced per epoch, which is what keeps them reconciling with cost_total.
    """
    acc: dict[str, dict] = {}
    for row in rows:
        _accumulate_model_row(acc, row)

    out = []
    for entry in acc.values():
        buckets = entry.pop("_buckets")
        total_in = entry["fresh"] + entry["cache_read"]
        entry["hit_rate_pct"] = round(
            (entry["cache_read"] / total_in * 100.0) if total_in else 0.0, 1
        )
        entry["cost_total"] = round(entry["cost_total"], 4)
        entry["cost_buckets"] = {k: round(v, 4) for k, v in buckets.items()}
        out.append(entry)
    out.sort(key=lambda e: e["cost_total"], reverse=True)
    return out
