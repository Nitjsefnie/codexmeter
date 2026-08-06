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
2. DEFAULT — anything else, priced at the cheapest, oldest model and
   reported as kind="default" so callers can mark the figure estimated
   rather than presenting it as fact.

claudit's middle rung (TIER — an unrecognised model falls back to its
family's current rates) has no codexmeter analogue: a kimi id carries no
family name to fall back on, and inventing one generation's rates for an
unknown id is exactly the silent mispricing rung 1 exists to prevent. The
kind vocabulary still reserves "tier" so the two repos agree on the field.

Every model parse.py can emit MUST have an entry in one of the two rate
tables. A missing entry does not raise — resolve() returns DEFAULT_RATES and
undercounts cost — but it does come back flagged estimated.
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# Order: most-specific first.
#
# MIRRORED IN src/parser.js — the in-browser Inspector carries its own copy of
# THIS table, and tests/test_parser_js_mirror.py asserts the two are identical,
# key for key and in order. Adding a model here is therefore a two-file change.
MODEL_RATES = {
    "kimi-k3":        {"fresh": 3.00, "create": 0.00, "read": 0.30, "output": 15.00},
    "kimi-k2-7-code": {"fresh": 0.95, "create": 0.00, "read": 0.19, "output": 4.00},
    "kimi-k2-6":      {"fresh": 0.95, "create": 0.00, "read": 0.16, "output": 4.00},
}

# Codex / OpenAI public API list rates as of the 2026-07-30 price cut (Terra
# -20%, Luna -80%; Sol held at its launch rate). Cache reads keep the 90%
# discount; explicit cache writes bill at 1.25x uncached input — unlike the
# Kimi models, where cache creation is free.
#
# These are LIST rates by design. The sessions they price ran on a ChatGPT
# subscription (rate_limits.plan_type is plus/pro), so the figure is what the
# same work would have cost through the API — the comparable number, not an
# invoice line.
#
# Keys are stored ALREADY NORMALISED (see _normalise): "gpt-5.6-sol" reaches
# the matcher as "gpt-5-6-sol", so a key spelled with the dot would never
# match its own model.
#
# WHY THIS IS A SEPARATE TABLE, not three more MODEL_RATES entries: MODEL_RATES
# is mirrored byte for byte by src/parser.js, whose Inspector cannot read a
# Codex rollout at all — it parses Kimi wire.jsonl only. Folding these in would
# break that parity contract in exchange for nothing the browser can use.
# resolve() consults BOTH tables, so every backend reader — including
# api._per_model, which prices straight off a DB row — prices a Codex model
# correctly. WHEN THE FRONTEND IS PORTED TO CODEX, mirror this table in
# src/parser.js and extend test_parser_js_mirror.py to cover it.
CODEX_MODEL_RATES = {
    "gpt-5-6-sol":    {"fresh": 5.00, "create": 6.25, "read": 0.50, "output": 30.00},
    "gpt-5-6-terra":  {"fresh": 2.00, "create": 2.50, "read": 0.20, "output": 12.00},
    "gpt-5-6-luna":   {"fresh": 0.20, "create": 0.25, "read": 0.02, "output": 1.20},
}

DEFAULT_RATES = MODEL_RATES["kimi-k2-6"]

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

    Both tables are searched, Kimi first. They share no prefix, so the order
    only fixes which one a future colliding key would win in.
    """
    for table in (MODEL_RATES, CODEX_MODEL_RATES):
        for key, rates in table.items():
            if not norm.startswith(key):
                continue
            rest = norm[len(key):]
            if rest == "" or rest[0] in "[@" or _SNAPSHOT_SUFFIX.match(rest):
                return key, rates
    return None


def resolve(model: str | None) -> Resolution:
    """Resolve a model id to rates, reporting how confident the match is."""
    match = _match_key(_normalise(model))
    if match is not None:
        return Resolution(match[1], "exact", match[0])
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
    long_context: bool = False,
) -> float:
    """USD cost for one request's token tally.

    long_context applies the Codex long-context meter (2x input, 1.5x
    output) to the whole request. It defaults off, and no Kimi caller
    passes it: the Kimi wire format has no such tier.
    """
    r = rate_for(model)
    in_mult = LONG_CONTEXT_INPUT_MULT if long_context else 1.0
    out_mult = LONG_CONTEXT_OUTPUT_MULT if long_context else 1.0
    return (
        fresh * r["fresh"] * in_mult / 1_000_000
        + create * r["create"] * in_mult / 1_000_000
        + read * r["read"] * in_mult / 1_000_000
        + output * r["output"] * out_mult / 1_000_000
    )
