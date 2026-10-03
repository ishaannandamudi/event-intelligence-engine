# Uncertainty decomposition

Every intelligence result contains all eight independent components. Each has a
categorical state, sorted reason codes and input references. There is no master
confidence score or calibrated probability, and components do not overwrite one
another.

| Component | Evidence and qualifications |
| --- | --- |
| Source | EIE-11 source classes, family information, corroboration, conflicts, withdrawals and retractions. Primary source trust does not imply perfect extraction. |
| Entity | Exact stable IDs versus unresolved names/roles or missing entity context. |
| Classification | EIE-12 deterministic/model-assisted method, verified candidates, partial runs and extraction failures. Failed extraction is not evidence of no event. |
| Numeric | Sourced numeric fields, ranges, conflict and unresolved canonical values. |
| Event identity | Pinned EIE-11 alias-resolution decisions, unresolved candidates, coarse time and truncated search. EIE-13 does not merge events. |
| Materiality | Missing denominators/exposure, incompatible units, threshold policy, range ambiguity, graph qualification and disputed/non-final status. |
| Analogue | `NOT_COMPUTED`, `INSUFFICIENT_INPUTS` or `READY_FOR_ANALOGUE_SEARCH`. Readiness checks only minimally sufficient identity and event evidence; it is not a confidence level or actual search. |
| Impact | `NOT_COMPUTED` when absent. Actual retained EIE-9 results supply model fit and contamination/data/uncertainty diagnostics in a separate component or audit. |

For the first six components and available impact diagnostics, `LOW`, `MODERATE`,
`HIGH`, `UNKNOWN` are categorical policy descriptions. Unknown is explicit when
the necessary inputs are absent. Analogue readiness remains a different vocabulary.
EIE-9 diagnostics describe a model and supplied market evidence; they do not raise
source trust or factual status. No post-event reaction improves an ex-ante
extraction or materiality judgment.

An accepted source can simultaneously have LOW source uncertainty, HIGH entity
uncertainty, MODERATE numeric uncertainty and UNKNOWN materiality. Callers must
inspect the relevant component and reasons. No trading, portfolio sizing or
execution logic consumes these states here.
