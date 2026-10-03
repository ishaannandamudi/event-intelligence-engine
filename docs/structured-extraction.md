# Structured source extraction

The pipeline is document → mapped normalization → segments → deterministic mentions
and event proposals → optional model proposals → verification → EIE-11 claims.
All immutable extraction contracts use text-extraction schema `1.0`.

## Structured facts

Numeric mentions use EIE-5 `Quantity`, `Unit` and canonical conversions. Supported
literals include explicit currency, thousand/million/billion, percent, basis points,
per-share money, signed/parenthesized numbers and compatible closed ranges.
`AT_LEAST`, `MORE_THAN`, `AT_MOST` and `LESS_THAN` stay distinct. Exclusive bounds
remain extraction artifacts because EIE-11's inclusive range contract cannot safely
represent them. The dollar symbol needs configured currency. Context binding uses
metric labels and preceding actual/guidance cues, not just the nearest number.
GAAP/non-GAAP, adjusted, basic/diluted and gross/operating qualifiers remain part of
the structured measurement definition and semantic measurement ID.
Integral literals retain integer precision. Unsupported fractional precision is
omitted instead of rounded. Conflicting range units, implicit endpoint scale and
competing values for one metric remain ambiguous and cannot verify as point facts.

Temporal mentions retain exact offset timestamp, date-only, date-plus-session or
unknown precision. Yearless month/day and next-quarter references stay unresolved.
Today/tomorrow require publication time plus a qualified source timezone. Exact
session labels are included in their supporting span. A coarse date cannot become
an exact timestamp. Candidate event/effective time needs an explicit supported
context; publication and reference-period dates are not silently event times.
Expected-close dates can remain in a nonfactual M&A candidate.

Reference periods require explicit fiscal year/quarter and period-end date. The
parser does not fabricate a calendar quarter end. Unqualified, incompatible or
ambiguous measurements remain partial/unverified artifacts.

Entity mentions reuse SEC CIK and historical ticker/listing identities. Use
`EntityCatalog.from_registry(registry, issuer_ids, known_as_of_ns, valid_at_ns)` to
pin a known snapshot. Exact identifiers need known issuer authority and ticker
validity at source time. Names supply candidates only, even with one matching name.
Unresolved/ambiguous mentions survive for audit. Person-role patterns retain literal
CEO/CFO/director/auditor names without creating a people graph.

Explicit management clauses can produce separate candidates within one segment.
Person, role, action and effective time stay bound to their own clause. The complete
segment remains supporting context, so clause separation cannot hide an instruction,
denial or rumor. Multiple issuer applicability or ambiguous undelimited changes
remain partial. Candidates share the document's provenance without sharing an event
identity. A repeated complete person/role/action/effective-time identity can share
a source-local event; repeated undated mentions are conservatively separate.
Model proposals must preserve that complete source identity, including its supported
effective time, and cannot mix fields from different clauses. EIE-11's existing
cross-source matching rules remain responsible for aliases.

## Evidence and verification

`EvidenceSpan` pins document, map, segment, normalized/original offsets and both
fragment hashes. One claim can depend on multiple spans. EIE-11 references point to
original CHARACTER locations; the model response never serves as source evidence.
The verifier requires a complete source segment to preserve negation and context.

Verification compares numeric value, canonical unit, bound, metric/qualifiers,
period, resolved entities, time precision and literal supported text fields against
deterministic source mentions. Cropped or bad-hash spans, invented entities,
numeric hallucinations, wrong nearby numbers, unsupported classification and
status upgrades are rejected. Results are `VERIFIED`, `PARTIALLY_VERIFIED`,
`UNVERIFIED` or `REJECTED`, with fixed typed reasons. Only verified candidates enter
EIE-11. Extraction quality is categorical, not a calibrated probability and not
source reliability.

## Claims and boundaries

Source-local event envelopes and descriptors preserve distinct documents and common
bundle provenance. Reported events remain `ConfirmationState.REPORTED` regardless
of extraction quality or model version. Numeric facts use exact issuer, period,
metric definition and canonical unit to identify an EIE-11 proposition slot.
The source-neutral measurement referent identifies that exact proposition; source
canonical envelopes are linked by `ProviderClaimObservation.event_id` and descriptors.
It does not select an event alias or create an extra event revision. Cross-source
value conflicts, source independence and event deduplication belong to EIE-11.

Verified earnings/guidance feed existing numeric/context/range contracts. Supported
buybacks, per-share dividends and person/role changes also emit EIE-4 `ActionValue`
payloads through EIE-11. Other monetary fields remain grounded candidate fields
when no existing EIE-5 metric/action contract fits. No consensus, surprise, severity,
materiality, source-trust upgrade or permanent relationship edge is inferred.

`TextQueries` exposes cutoff-bounded documents, runs, candidates, mentions, failures,
accepted assertions and event audits. `audit(run_id, known_as_of_ns)` traces
candidate → verification → claim/assertion → original evidence → segment/map →
document → raw observation. Audits omit source bodies.
