"""Per-model cost rates (USD per million tokens).

SINGLE SOURCE OF TRUTH for cost in kimimeter.
Bump PARSER_VERSION when this table changes — every session reparses.

Kimi wire format token categories:
  - fresh  = input_other        (vendor: "input price, cache miss")
  - read   = input_cache_read   (vendor: "input price, cache hit")
  - create = input_cache_creation
  - output = output

No TTL split in Kimi wire format; cache_create is billed at a flat rate.

Two resolution behaviours matter, in priority order:

1. EXACT — the model id matches a key in MODEL_RATES, allowing only a
   bracket, an "@" or a dated-snapshot suffix after it. The match is
   ANCHORED at the start of the id, which is the whole point: a substring
   test bills a model the table has never heard of at the rates of whatever
   key happens to sit inside its name. A hypothetical "kimi-k2-6-turbo"
   would take retired kimi-k2-6 pricing, and "kimi-k3-mini" would take K3's
   ~3x rates — silently, with nothing in the payload to say the figure was
   guessed.
2. DEFAULT — anything else, priced at the cheapest, oldest model and
   reported as kind="default" so callers can mark the figure estimated
   rather than presenting it as fact.

claudit's middle rung (TIER — an unrecognised model falls back to its
family's current rates) has no kimimeter analogue: a kimi id carries no
family name to fall back on, and inventing one generation's rates for an
unknown id is exactly the silent mispricing rung 1 exists to prevent. The
kind vocabulary still reserves "tier" so the two repos agree on the field.

Every model parse.py can emit MUST have an entry here. A missing entry does
not raise — resolve() returns DEFAULT_RATES and undercounts cost — but it
does come back flagged estimated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Order: most-specific first.
MODEL_RATES = {
    "kimi-k3":        {"fresh": 3.00, "create": 0.00, "read": 0.30, "output": 15.00},
    "kimi-k2-7-code": {"fresh": 0.95, "create": 0.00, "read": 0.19, "output": 4.00},
    "kimi-k2-6":      {"fresh": 0.95, "create": 0.00, "read": 0.16, "output": 4.00},
}

DEFAULT_RATES = MODEL_RATES["kimi-k2-6"]

# A dated snapshot suffix ("-20260601") names the same model at a pinned
# revision; a version suffix ("-8") or a mode suffix ("-turbo") names a
# DIFFERENT model, which the table cannot price.
_SNAPSHOT_SUFFIX = re.compile(r"^-?\d{6,8}$")


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving a model id to rates.

    kind: "exact" | "default" (claudit also emits "tier"). Anything other
    than "exact" means the figure is an estimate and should be surfaced as
    such rather than shown as a billed fact.
    """
    rates: dict
    kind: str
    key: str | None = None

    @property
    def estimated(self) -> bool:
        return self.kind != "exact"


def _normalise(model: str | None) -> str:
    """Case-fold and normalise version separators: "Kimi-K2.7-code" and
    "kimi-k2-7-code" name the same model. None (a NULL model off a DB
    row) normalises to "" and lands on the default rates.

    Deliberately does NOT strip a provider prefix the way claudit's
    normaliser does. parse._canonical_model already reduces raw wire ids
    ("kimi-code/k3") to canonical labels, and a raw id that reaches pricing
    is a bug — it must keep resolving to the default rather than being
    quietly repaired here, which is what test_parse.py's
    test_kimi_code_k3_record_is_priced_at_k3_rates guards.
    """
    return (model or "").strip().lower().replace(".", "-")


def _match_key(norm: str) -> str | None:
    """The table key this id names exactly, or None.

    Anchored at the start, and the remainder must be nothing, a bracket, an
    "@" or a dated snapshot. No key here is a prefix of another, but the
    table stays most-specific-first so that a future one can be.
    """
    for key in MODEL_RATES:
        if not norm.startswith(key):
            continue
        rest = norm[len(key):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            return key
    return None


def resolve(model: str | None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is."""
    key = _match_key(_normalise(model))
    if key is not None:
        return Resolution(MODEL_RATES[key], "exact", key)
    return Resolution(DEFAULT_RATES, "default")


def rate_for(model: str | None) -> dict:
    """Rates for a model. Callers that render cost should prefer resolve(),
    whose .estimated says whether the rates were matched or assumed.
    """
    return resolve(model).rates


def compute_cost(
    model: str,
    *,
    fresh: int,
    create: int,
    read: int,
    output: int,
) -> float:
    """USD cost for one StatusUpdate token tally."""
    r = rate_for(model)
    return (
        fresh * r["fresh"] / 1_000_000
        + create * r["create"] / 1_000_000
        + read * r["read"] / 1_000_000
        + output * r["output"] / 1_000_000
    )
