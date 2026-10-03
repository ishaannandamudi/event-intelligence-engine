# Novelty intelligence (EIE-13)

`eie.intelligence` adds an immutable event-intelligence schema `1.0` beside EIE-11
claims, EIE-12 extraction, EIE-5 surprise and EIE-9 empirical impact. The engine
version is `1.4.0`. Novelty is descriptive information change, with six separate
dimensions, source references, reason codes and explicit missing metrics.

| Dimension | Calculation and qualification |
| --- | --- |
| Document | Original/normalized hash equality, segment recurrence and accepted structured-field recurrence. Exact repeats have zero new-segment fraction. Normalization versions must match for segment comparisons. |
| Factual | Exact semantic proposition slots compare resolved prior fields. New facts, updates, corrections, confirmations, restatements, retractions and unresolved evidence remain distinct. |
| Numeric | Compatible prior reporting periods supply changes, bounds, median, median absolute deviation and standardized MAD. |
| Guidance | Prior company guidance for the same issuer, metric, period, stage, seasonality and unit supplies low/high/midpoint/width displacement. Initiation and withdrawal retain missing comparisons. |
| Entity | Exact stable identities are compared with prior claims and an explicitly pinned prior EIE-10 graph. Unresolved names remain unresolved. |
| Cross source | Existing source families distinguish syndicated repeats, independent confirmation, contradictions and primary/secondary new information. EIE-11 owns resolution and alias decisions. |

## Baseline selection

The request pins event ID, exact revision, dataset/build, policy and an explicit
knowledge cutoff `T`. Independent numeric samples require knowledge strictly
before `T`, an earlier non-overlapping reporting period, compatible event type,
entity, measurement, units, release stage, seasonality and actual-role semantics.
The current event and EIE-11 alias group are excluded. Same-time records never
become prior samples. Older same-proposition vintages can still explain factual
corrections and guidance revisions.

Repeated feeds do not inflate sample count: a compatible reporting period contributes
one deterministically selected observation. Competing period values are excluded
with `AMBIGUOUS_PERIOD_BASELINE_EXCLUDED`. The policy bounds the latest chronological
sample window. Ranges retain bounds; numeric novelty never invents a point.
Near-zero denominators, absent points, insufficient samples, zero MAD and arithmetic
overflow return `None` and a reason, rather than infinity or a fabricated zero.

These metrics are not consensus surprise, calibrated probabilities, empirical
price impact or trading signals. No embeddings, external model, analyst consensus
builder or analogue retrieval is required. Every dimension is present, including
explicit placeholder states when its inputs are absent.

See [materiality](materiality-intelligence.md), [uncertainty](uncertainty-decomposition.md)
and [replay](intelligence-replay.md).

## EIE-14 consumer

Historical analogue features can read an exact frozen numeric/guidance novelty metric, with an explicit measurement and matching semantic/unit signature. Candidate features use their own ex-ante source cutoff. Retrieval does not rebuild novelty using later history. See [distance](analogue-distance.md).
