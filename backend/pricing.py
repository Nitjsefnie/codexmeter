"""Per-model cost rates (USD per million tokens).

SINGLE SOURCE OF TRUTH for cost in codexmeter.
Bump PARSER_VERSION when this table changes — every session reparses.

Kimi wire format token categories:
  - fresh  = input_other        (vendor: "input price, cache miss")
  - read   = input_cache_read   (vendor: "input price, cache hit")
  - create = input_cache_creation
  - output = output

Codex rollout token categories map onto the same four buckets, but only
after the subtraction the format demands — cached_input_tokens and
cache_write_input_tokens are SUBSETS of input_tokens, not addends:
  - fresh  = input_tokens - cached_input_tokens - cache_write_input_tokens
  - read   = cached_input_tokens
  - create = cache_write_input_tokens
  - output = output_tokens

No TTL split in either format; Kimi cache_create is billed at a flat rate
(zero), Codex cache writes at 1.25x uncached input.

Two resolution behaviours matter, in priority order:

1. EXACT — the model id matches a key in MODEL_RATES, allowing only a
   bracket, an "@" or a dated-snapshot suffix after it. The match is
   ANCHORED at the start of the id, which is the whole point: a substring
   test bills a model the table has never heard of at the rates of whatever
   key happens to sit inside its name. A hypothetical "kimi-k2-6-turbo"
   would take retired kimi-k2-6 pricing, and "kimi-k3-mini" would take K3's
   ~3x rates — silently, with nothing in the payload to say the figure was
   guessed.
2. DEFAULT — anything else, priced at the cheapest Codex model and
   reported as kind="default" so callers can mark the figure estimated
   rather than presenting it as fact.

claudit's middle rung (TIER — an unrecognised model falls back to its
family's current rates) has no codexmeter analogue: a kimi id carries no
family name to fall back on, and inventing one generation's rates for an
unknown id is exactly the silent mispricing rung 1 exists to prevent. The
kind vocabulary still reserves "tier" so the two repos agree on the field.

Every model either parser can emit MUST have an entry in MODEL_RATES. A
missing entry does not raise — resolve() returns DEFAULT_RATES and
undercounts cost — but it does come back flagged estimated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone

UTC = timezone.utc


# Order: most-specific first.
#
# MIRRORED IN src/parser.js — the in-browser Inspector carries its own copy of
# THIS table, and tests/test_parser_js_mirror.py asserts the two are identical,
# key for key and in order. Adding a model here is therefore a two-file change.
MODEL_RATES = {
    "kimi-k3":        {"fresh": 3.00, "create": 0.00, "read": 0.30, "output": 15.00},
    "kimi-k2-7-code": {"fresh": 0.95, "create": 0.00, "read": 0.19, "output": 4.00},
    "kimi-k2-6":      {"fresh": 0.95, "create": 0.00, "read": 0.16, "output": 4.00},
    # Codex / OpenAI CURRENT list rates. Everything the family charged
    # before them is in DATED_RATES below. In every window of every tier a
    # cached read is 0.1x uncached input and a cache write 1.25x it, which
    # is how the create/read columns here are derived — confirmed against
    # OpenAI's own Sol table, which prints 4 / 0.4 / 5 / 20.
    "gpt-5.6-sol":    {"fresh": 4.00, "create": 5.00, "read": 0.40, "output": 20.00},
    "gpt-5.6-terra":  {"fresh": 2.00, "create": 2.50, "read": 0.20, "output": 12.00},
    "gpt-5.6-luna":   {"fresh": 0.20, "create": 0.25, "read": 0.02, "output": 1.20},
}

DEFAULT_RATES = MODEL_RATES["gpt-5.6-luna"]

# The two GPT-5.6 repricings, as frozen UTC instants — NOT live expressions.
# Each is the moment of OpenAI's own @Product Updates post, which is the
# finest resolution available; the posts say "starting today" and carry no
# separate effective time. The source timestamps were read in Europe/Prague
# (CEST, UTC+2), so each is the posted wall clock minus two hours. If that
# reading is wrong the boundary moves by exactly that offset and nothing
# else about the mechanism changes.
JUL30_CUT = datetime(2026, 7, 30, 18, 12, tzinfo=UTC)   # 20:12 Europe/Prague
AUG21_CUT = datetime(2026, 8, 21, 19, 40, tzinfo=UTC)   # 21:40 Europe/Prague

# Dated overrides, per exact key: (end_exclusive_utc, rates). Applied only
# when a timestamp is supplied and only on an EXACT key match — the flagged
# default never inherits another model's retired price. Each window carries
# rates in full, the same shape as MODEL_RATES, because a partial override
# is the kind of thing that reads as complete and bills as half-updated.
#
# The family repriced twice, and each cut moved a different subset:
#   2026-07-09  GA                sol 5/30     terra 2.50/15   luna 1/6
#   2026-07-30  luna -80%, terra -20%          sol untouched
#   2026-08-21  sol -20% in / -33% out         terra and luna untouched
# Sol's cut is promotional, announced as running at least through
# 2026-11-21. Nothing is encoded for that: a reversion that has not happened
# is not a rate, and guessing one would silently overbill every record after
# the guessed date. Add a window when it actually moves.
#
# Ordered oldest-first, and _dated returns the FIRST window the timestamp
# falls before, so a key may carry several.
DATED_RATES: dict[str, list[tuple[datetime, dict]]] = {
    "gpt-5.6-sol": [
        (AUG21_CUT, {"fresh": 5.00, "create": 6.25,
                     "read": 0.50, "output": 30.00}),
    ],
    "gpt-5.6-terra": [
        (JUL30_CUT, {"fresh": 2.50, "create": 3.125,
                     "read": 0.25, "output": 15.00}),
    ],
    "gpt-5.6-luna": [
        (JUL30_CUT, {"fresh": 1.00, "create": 1.25,
                     "read": 0.10, "output": 6.00}),
    ],
}

# Sorted boundaries where any rate changes. Read-time aggregation that
# re-derives cost from summed tokens MUST group by these — see
# api.rate_epoch_sql — or its per-component breakdown drifts from the
# per-record cost it claims to decompose.
RATE_EPOCHS: list[datetime] = sorted(
    {end for windows in DATED_RATES.values() for end, _ in windows}
)

# A Codex request whose prompt exceeds this size bills the WHOLE request at
# the long-context meter: 2x input, 1.5x output. Applied per record by the
# caller, which is the only place that knows the request's prompt size.
LONG_CONTEXT_THRESHOLD = 272_000
LONG_CONTEXT_INPUT_MULT = 2.0
LONG_CONTEXT_OUTPUT_MULT = 1.5

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


def _match_key(norm: str) -> tuple[str, dict] | None:
    """The (key, rates) this id names exactly, or None.

    Anchored at the start, and the remainder must be nothing, a bracket, an
    "@" or a dated snapshot. No key is a prefix of another, but each table
    stays most-specific-first so that a future one can be.

    Table keys are canonical display labels. Normalise each key for comparison
    rather than storing an internal spelling that no parser can emit.
    """
    for key, rates in MODEL_RATES.items():
        norm_key = _normalise(key)
        if not norm.startswith(norm_key):
            continue
        rest = norm[len(norm_key):]
        if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
            return key, rates
    return None


def _dated(key: str, ts: datetime | None) -> dict:
    """The rates `key` charged at `ts`, or its current rates.

    Boundaries are strictly-before / inclusive-at, so a cut instant belongs
    to the NEW price — the same convention parse.py's model cutoffs use. A
    naive datetime is read as UTC: psycopg can hand one back, and treating
    it as server-local would move every boundary by the machine's offset.
    """
    windows = DATED_RATES.get(key)
    if not windows or ts is None:
        return MODEL_RATES[key]
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    for end_exclusive, rates in windows:
        if ts < end_exclusive:
            return rates
    return MODEL_RATES[key]


def resolve(model: str | None, ts: datetime | None = None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is.

    `ts` is when the tokens were spent. Omitting it yields current list
    price, which is what a caller with no record in hand (a filter check, a
    rate display) wants.
    """
    match = _match_key(_normalise(model))
    if match is not None:
        return Resolution(_dated(match[0], ts), "exact", match[0])
    return Resolution(DEFAULT_RATES, "default")


def rate_for(model: str | None, ts: datetime | None = None) -> dict:
    """Rates for a model at a point in time. Callers that render cost should
    prefer resolve(), whose .estimated says whether the rates were matched
    or assumed. Omitting ts yields current list price.
    """
    return resolve(model, ts).rates


def compute_cost(
    model: str,
    *,
    fresh: int,
    create: int,
    read: int,
    output: int,
    long_context: bool = False,
    ts: datetime | None = None,
) -> float:
    """USD cost for one request's token tally.

    long_context applies the Codex long-context meter (2x input, 1.5x
    output) to the whole request. It defaults off: no Kimi caller passes it
    (the wire format has no such tier), and neither does a Codex record on a
    subscription — see parse_codex._codex_token_count.

    Pass the record's own timestamp so a dated rate applies to when the
    tokens were spent rather than to when the row is being read.
    """
    r = rate_for(model, ts)
    in_mult = LONG_CONTEXT_INPUT_MULT if long_context else 1.0
    out_mult = LONG_CONTEXT_OUTPUT_MULT if long_context else 1.0
    return (
        fresh * r["fresh"] * in_mult / 1_000_000
        + create * r["create"] * in_mult / 1_000_000
        + read * r["read"] * in_mult / 1_000_000
        + output * r["output"] * out_mult / 1_000_000
    )
