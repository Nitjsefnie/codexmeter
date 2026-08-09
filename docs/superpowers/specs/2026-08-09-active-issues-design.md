# Active Issues Design

## Scope

Resolve every issue open in `Nitjsefnie/codexmeter` on 2026-08-09: #1,
#3, #4, #5, and #6. If implementation reveals an unrelated defect, record
it as a new GitHub issue and do not fix it in this change set.

## Model catalog and pricing (#1, #6)

The application ingests both Kimi wires and Codex rollouts, so rate lookup
must support both ecosystems. `backend/pricing.py::MODEL_RATES` will become
the single mirrored table for all six canonical models. `src/parser.js`
will carry the same entries and matching behavior. This is necessary even
though the browser parser does not parse Codex rollouts: the live dashboard
uses `window.rateForModel` to calculate its token-cost breakdown from Codex
API rows.

Codex rates remain the values already established in `backend/pricing.py`:

| Model | Fresh | Cache write | Cache read | Output |
|---|---:|---:|---:|---:|
| `gpt-5.6-sol` | 5.00 | 6.25 | 0.50 | 30.00 |
| `gpt-5.6-terra` | 2.00 | 2.50 | 0.20 | 12.00 |
| `gpt-5.6-luna` | 0.20 | 0.25 | 0.02 | 1.20 |

The Luna values intentionally preserve the 80% price reduction. The generic
estimated fallback will be Luna, the cheapest canonical Codex model. Kimi
records emitted by the parser continue to resolve exactly against their Kimi
entries; Kimi-specific date and wire-model classification in `src/parser.js`
is format logic and will not be relabeled as Codex.

The demo generator will use the same Codex table values. Context-growth
fallbacks will contain all supported Kimi values plus 272,000-token limits for
Sol, Terra, and Luna, as confirmed by the local Codex model catalog. Empty
model selections will use `unknown`, not an impossible Kimi default.

The tool endpoints cannot truthfully filter individual calls by model because
`tool_uses` and `tool_rollup` intentionally have no model dimension. A supplied
model remains a validity gate: any model resolved exactly by the shared pricing
catalog receives the available tool series, while an unknown model receives an
empty series. Filtered tool-error rows carry the requested model; unfiltered
rows carry `unknown` instead of an arbitrary model. This preserves the
documented caveat without inventing attribution.

Project identifiers in logging and operational switches will use
`codexmeter`: logger namespaces, timing, and warm-cache environment variables.
Database documentation/defaults and the schema banner will also use the
current project name. The records schema default model will become
`gpt-5.6-sol`; normal ingest already supplies a model explicitly.

## Time-series bucket contract (#3)

The server chooses `bucket_s` from the requested range so wide views can use
rollups. The frontend may choose a coarser display bin from the visible data
extent, but it must never choose a finer one than the pre-aggregated server
rows. Dashboard bin selection will therefore clamp to
`max(data-derived-bin, bucket_s * 1000)`.

Every time-series panel, including churn and cumulative lines, already shares
this bin width. The existing axis formatter will consequently describe the
actual width. Request parameters, cache keys, and rollup selection do not
change.

## Token-type contract (#4, #5)

The dashboard payload will describe token fields instead of requiring the
frontend to duplicate an implicit list. Each token-type descriptor contains:

- the hourly field name;
- its display label;
- whether it contributes to Total Tokens;
- its pricing bucket, or no pricing bucket when its cost is already included
  in a parent type.

Fresh input, output, cache read, and cache write are total addends. Reasoning
output is a subset of output and is displayed without being added to Total
Tokens or cost a second time. Zero-suppression remains response-wide: a type
with a zero range total is absent from both hourly rows and token metadata.

`backendDashToShape` will copy the token fields named by metadata into each
synthetic event. A missing optional field is zero. A present non-finite value
is an invalid payload and must fail loudly rather than becoming a plausible
zero series. Totals, breakdown rows, and time-series panels will be generated
from the descriptors. Cache writes therefore appear automatically when
nonzero, reasoning output receives its own series, and absent cache reads
cannot introduce `undefined` or `NaN`.

Frontend styling remains frontend-owned through a small field-to-color map.
Unknown future token types receive a deterministic fallback color while their
total and billing behavior still comes from backend metadata.

## Tests and commits

Each behavior change starts with a regression test that fails for the current
implementation. Node probes will exercise the real in-browser JavaScript for
rate parity, payload normalization, token totals, dynamic panel definitions,
and bucket selection. Python tests will cover token metadata, model-filter
validity, truthful unfiltered labels, renamed project identifiers, schema
defaults, and pricing parity.

The existing full pytest suite is the final regression gate. Parser semantics
and mirrored pricing change, so `PARSER_VERSION` will be bumped from 8 to 9 in
the deployment environment. Commits will include
`Co-authored-by: GPT-5.6 Sol <noreply@openai.com>` and the implementation
commits will include every applicable `Closes #N` trailer.
