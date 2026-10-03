# Static importance and event materiality

Static importance is a versioned event-family/type lookup with categories
`LOW`, `MODERATE`, `HIGH`, `CRITICAL`. Defaults assign HIGH to macro, central bank,
earnings and regulatory/legal events and MODERATE to the other families. Explicit
type overrides take precedence. This policy describes general importance.

Event materiality is separately calculated from the particular supplied event.
Its categories are `UNKNOWN`, `MINOR`, `MODERATE`, `MAJOR`, `VERY_HIGH`.
Every contribution retains its subject, metrics, input IDs and reasons.

| Contribution | Inputs and behavior |
| --- | --- |
| Financial amount | Source-verified EIE-12 amounts, EIE-11 numeric currency fields or existing corporate action money contracts. Defaults: USD thresholds 10 million, 100 million, 1 billion; magnitude uses absolute value. Per-share values have no total-company threshold by default. |
| Company scale | An explicitly selected available, resolved same-issuer revenue/assets assertion with matching canonical currency. Positive scale is required; missing, near-zero or incompatible scale stays unknown. Defaults: 1%, 5%, 20%. |
| Guidance displacement | Prior-company guidance relative midpoint change. Defaults: 1%, 5%, 20%; initiation, withdrawal and unavailable comparisons retain reasons. |
| Scope and affected entities | Supplied canonical scope and count of exact stable identities; unresolved entities remain qualified. Counts do not fabricate financial severity. |
| Status | Existing claim and extraction epistemic states retain planned, conditional, rumored, denied, pending and retracted qualifications. |
| Exposure | Only active direct relationships in an explicitly pinned current EIE-10 snapshot touching an event entity. Defaults: 5%, 20%, 50%; measurement type, estimated exposure, conflicts and truncation remain visible. |
| Duration | A supplied buyback expiration date can yield a calendar-day gap from the UTC announcement date. Date-only/timezone qualification remains explicit; no exact expiration time is invented. |

## Combination and missingness

The named policy rule `MAX_AVAILABLE_FINANCIAL_SCALE_GUIDANCE_OR_EXPOSURE` chooses
the highest available category among those four eligible contributions. Scope,
status, entity counts and duration remain separate descriptive contributions.
Missing eligible contributions flag `PARTIAL_INPUTS`. If none has a supported
category, the result is `UNKNOWN`. The rule is configurable only through explicit
versioned threshold policies; unsupported opaque score rules fail validation.

Closed ranges are categorized only when their full magnitude interval fits one
category. A range crossing categories is unknown even if it supplies a point.
Open ranges retain unknown category and bounds. Missing exposure or denominator
never becomes zero. No market cap fetch, implicit FX conversion, graph weight,
revenue-share estimate or scope expansion is performed. Signed amounts retain
their signed metrics while thresholds use magnitude.

EIE-5 surprise and EIE-9 abnormal returns are separate references. Neither feeds
this calculation. Adding an impact audit cannot change the original materiality,
importance, novelty or extraction quality. These categories are descriptive policy
outputs, not forecasts or recommendations.

## EIE-14 consumer

Analogue policies can request categorical materiality or a named financial/scale/guidance magnitude component from immutable ex-ante EIE-13 results. Later market response never changes these features. Missing or ambiguous metrics stay unavailable. See [historical analogues](historical-analogues.md).


## EIE-15 interaction consumer

EIE-15 stores descriptive interaction sets/counts. Individual materiality artifacts stay separate per event. Colliding MAJOR/VERY_HIGH events produce no summed or ranked combined severity. Changing a materiality policy cannot create a collision or reorder an event sequence.
