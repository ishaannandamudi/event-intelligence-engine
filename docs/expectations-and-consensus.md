# Expectations and consensus (EIE-5)

Engine `0.6.0` adds expectations schema `1.0`. Existing schemas remain `1.0`.
The implementation uses the Python standard library and synthetic offline extracts.
`ExpectationsSettings.enabled` defaults to `False`.

## Event, measurement, and period identity

`ExpectationsEngine(settings, event_store, registry)` owns copies of an existing
canonical event store and SEC entity registry. Call `bind_event(event_id, period,
as_of_ns)` before ingesting observations. The event must be known at that time;
company/security scopes require verified stable identities. Multi-issuer scopes
are rejected. Earnings require an `EarningsPeriod`; macro events require a
`ReferencePeriod` with exclusive end date. Binding verifies period facts and
release stage already present in the canonical event. A conflicting rebind fails.
The engine does not infer a period, match ticker text, synchronize later changes
from the upstream store, or turn an actual into a canonical lifecycle update.

Every observation contains a `MeasurementContext`: canonical event ID, hierarchical
`MeasurementId`, exact reference period, optional release stage, and typed seasonal
adjustment. EPS, revenue, gross margin, operating margin, CPI headline/core MoM/YoY,
NFP payroll change, unemployment, wages, core PCE MoM, and annualized real GDP QoQ
have separate IDs. Additional `EARNINGS.*`, `MACRO.*`, and `GUIDANCE.*` IDs are
supported. Stages and seasonal adjustment remain separate series. Guidance uses
its target period, which may differ from the event's reporting period.

## Values and units

`Quantity` retains the reported numeric value and `Unit` together with canonical
value and `CanonicalUnit`. Booleans, strings, NaN, infinity, and conversion overflow
are rejected. Monetary values require a three-letter uppercase currency. No FX
conversion is performed.

| Reported unit | Canonical representation |
| --- | --- |
| Currency / currency per share | Distinct currency-qualified dimensions |
| Money or count in thousands, millions, billions | Multiply by 1,000 / 1,000,000 / 1,000,000,000 |
| Percent | Fractional rate: `0.3%` becomes `0.003` |
| Percentage points / basis points | Fractional rate change: `1 pp` = `100 bp` = `0.01` |
| Annualized growth | Fractional annualized rate, separate from ordinary rates |
| Index level / ratio | Separate unscaled dimensions |

A ratio is not automatically a percent; a rate is not automatically a rate change.
Measurement, period, stage, adjustment, dimension, and currency must be compatible.
For two percent-reported levels, a canonical difference of `0.001` represents
`0.1` percentage point. Arithmetic uses finite Python integers/floats and does not
claim exact decimal financial accounting.

## Immutable observations and vintages

`EstimateObservation` has context, `ObservationSource`, quantity, `EstimateKind`,
`EventRevision`, optional stable contributor ID/count, immutable quality flags,
schema version, stable stream ID, observation ID, and result fingerprint.
`ObservationSource` retains provider ID, source item ID, provider record ID, raw
observation ID, source class, provider observation/ingestion/availability clocks,
and optional vintage timestamp. The clocks require
`vintage <= provider_observed <= ingested <= available` when a vintage exists.
Revision availability must match source availability; stored raw evidence must
match provider, item, class, and observation/ingestion clocks.

Observation history is append-only. A changed source item for the same provider
record appends a contiguous correction; availability must advance. An identical
raw item is suppressed, including after intervening corrections. Different
providers remain independent. A provider record ID cannot change measurement,
period, contributor, or role identity. The synthetic adapter requires stable IDs;
it does not invent anonymous analyst identities.

The latest value at `T` is the latest eligible observation with availability
`<= T`. A historical vintage delivered later remains unavailable before delivery.
A vintage timestamp is required for `point_in_time=True`. A current snapshot
without a vintage remains queryable and is flagged `MISSING_TIMESTAMP` /
`NON_POINT_IN_TIME_PROVENANCE`; surprise calculation fails closed.

## Two consensus origins

`ConsensusOrigin.PROVIDER` selects reported mean, median, high, low, or count.
Latest eligible claims for each provider remain in `provider_claims`. Selection
uses source precedence override `consensus.<lowercase_measurement_id>`, then stable
provider ID for ties. Disagreement in value/unit is flagged `CONFLICTING_SOURCES`
and `CONFLICTING_PROVIDERS`. Providers are never averaged together.
Supplementary high/low/count observations belong to the selected provider and
are independently available evidence. Incompatible statistics are explicitly
flagged; dispersion and IQR are not fabricated from provider consensus.

`ConsensusOrigin.ENGINE` requires an explicit provider. Only individual estimates
with stable contributor IDs participate. The latest eligible row per contributor
is used; the identity namespace is provider plus contributor. Anonymous rows are
retained in history but excluded with `CONTRIBUTOR_ID_MISSING` and degraded quality.
Mean, median, count, high, and low are supported. Compatible individual estimates
provide sample standard deviation with at least two contributors and IQR with
at least four (median of each sorted half, excluding the middle for odd counts).
`spread` derives high minus low when finite. Count queries return a count unit;
they do not make a monetary surprise. Overflow returns missing statistics with
`NUMERIC_OVERFLOW`.

`ConsensusSnapshot` exposes origin, method, selected provider, value/unit, count,
high/low, dispersion, IQR, age, point-in-time status, selected inputs, competing
claims, coverage, health, flags, reasons, as-of time, configuration fingerprint,
and result fingerprint. Age is `as_of - oldest contributing available_from`;
a fresh count cannot conceal an old mean or analyst contribution. Staleness is
strictly `age > staleness_ns`. A stale value stays present and flagged.

## Coverage, health, and missingness

| Availability | Meaning |
| --- | --- |
| `AVAILABLE` | Eligible consensus exists |
| `STALE_ESTIMATE` | Consensus exists but its age exceeds the configured threshold |
| `NO_ESTIMATE_AVAILABLE` | Explicit provider coverage exists, with no eligible estimate |
| `PROVIDER_UNAVAILABLE` | A relevant provider has failed or degraded, with no eligible estimate |
| `COVERAGE_UNKNOWN` | No eligible estimate and no matching coverage assertion |

Healthy empty input alone does not establish coverage. `EstimateCoverage` asserts
specific event IDs, measurement IDs, vintage capability, and knowledge time;
it is not universal completeness. Health is causal and can become stale after a
known successful attempt. Existing estimates survive outages and carry a source
unavailable flag. Latency and failure counters are diagnostic fields exposed by
the ingestor; snapshot health projects them out of result fingerprints.

## Offline adapter and atomic writes

`EstimateAdapter` declares `CONSENSUS`, `ANALYST`, `ACTUAL`, `GUIDANCE`, and/or
`VINTAGES` capabilities. `EIE_ESTIMATES_EXTRACT_V1` is an audited interchange
format, not a native commercial-vendor API. See `tests/fixtures/estimates` for
synthetic provider A/B and actual examples. Each item has a stable ID/version,
credential-free HTTPS reference and records. Records declare observation type,
stable record ID, event/measurement/period, typed numeric quantity/range, vintage,
and publication/contributor fields as applicable.

`EstimateIngestor` validates limits and schema, deep-copies the complete engine,
inserts immutable raw observations first, then validates observation/revision
chains and coverage. It commits one engine pointer only after the entire batch
succeeds. Failure leaves logical raw/observation/coverage state unchanged and
records causal diagnostics/health separately. Attempts must advance per provider;
different providers can be observed at the same instant.

Malformed JSON, unsupported schema/enums, unsupported capabilities, unresolved
events, conflicting duplicate records, and reversed clocks fail explicitly.
Credentials in URL userinfo/query or fragments are rejected. Provider text is
data, never executable code. There is no network or file access from the parser.

## Queries and configuration

- `estimates(event_id, measurement, as_of_ns)` — immutable observation history.
- `consensus(event_id, measurement, as_of_ns, provider_id=..., origin=..., method=...)`.
- `actuals(...)`, `guidance(...)` — separately typed histories.
- `surprise(...)`, `surprise_history(...)` — see [surprise intelligence](surprise-intelligence.md).
- `guidance_change(new)` and `guidance_vs_consensus(new)` — distinct comparisons.

`ExpectationsSettings` contains enabled, minimum history (default 5; at least 3),
MAD/sample-standard-deviation method, optional rolling window (at least minimum),
staleness (default 7 days), strict/inclusive cutoff policy, same-provider/explicit
pooling policy, relative surprise opt-in and minimum absolute denominator, and
payload/item limits. Result-relevant settings enter the configuration fingerprint.
No mutable online statistics, wall-clock reads, or persistent database are used.

The full runnable fixture example is [expectations_replay.py](../examples/expectations_replay.py).
Existing corporate and official ingestion can supply their canonical stores and
verified registry to the engine; [integration tests](../tests/test_expectations_integration.py)
exercise both paths.

## EIE-6 durability

Registered estimate processors can share one durable expectations namespace.
Serialized transactions load current state before merging providers, preventing
lost updates. Estimates, actuals, guidance, raw evidence, event/period bindings,
coverage and adapter identity caches survive restart. Consensus/surprise queries
use the existing immutable histories and cutoff rules. Operational transport
settings and telemetry do not alter their fingerprints. No licensed collector
is bundled. See [persistence](persistence.md) and [provider runtime](provider-runtime.md).

## Historical vintage inputs

EIE-7 audited structured histories can preserve estimate, actual and guidance
observations with provider vintages, publication times, units, periods and source
revision numbers. Dataset queries filter the wrapper's explicit source availability;
later source revisions never overwrite an older frozen build. Supporting domain
contracts keep genuine local acquisition clocks, preserving EIE-5 invariants. They
must not be backdated and inserted into a live expectations engine. Current snapshots
alone do not establish old consensus or macro correction history; exact historical
cutoffs require audited provider support. Licensed historical collection and exhaustive
macro vintage downloading remain absent. See [completeness](data-completeness.md).

## EIE-8 conditioning boundary

A supplied typed EIE-5 `SurpriseObservation` can condition descriptive study distributions.
It stays bound to its canonical event and measurement, retains its result fingerprint and
normalization history, and cannot be newer than the study anchor. Bucket boundaries are
explicit numeric settings; equality goes to the right bucket. There are no bullish/bearish
labels, future-calibrated quantiles or regime inference. See [event studies](event-studies.md).

## EIE-11 structured numeric evidence

Numeric claims reuse EIE-5 measurement context, canonical units, actual/previous/revised
roles, estimate kind/contributor and exact reference periods. These dimensions participate
in semantic identity, so an actual and prior value or a mean and median cannot collapse.
Guidance preserves supplied lower/upper/point values, including one-sided ranges, without
inventing a midpoint. Claim resolution retains provider alternatives and uses categorical
field-specific policy. Information surprise and consensus calculations remain unchanged.
See [claim resolution](claim-resolution.md).
