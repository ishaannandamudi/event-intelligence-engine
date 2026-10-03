# Structured claim/evidence graph (EIE-11)

Schema `1.0`, engine `1.2.0`. `evidence` is independent of the EIE-10 exposure graph.

```text
RawSourceObservation ← AcquisitionLineage (honest local acquisition and build)
        ↓
EvidenceReference (content/fragment hashes, parser, structured locator)
        ↓ many-to-many normalized links
Claim (semantic proposition slot) ← ProviderClaimObservation (assertion/revision)
        ↓ typed links and field-specific categorical policy
CanonicalClaimResolution (exact inputs, alternatives, conflicts, two-time cutoff)
```

A claim identifies a proposition, such as an issuer's earnings schedule for an exact
fiscal period. A provider observation asserts a particular value for that proposition.
A canonical event retains its existing EIE identity and revisions. Resolving claims or
event aliases does not delete, rewrite or physically combine source events.

## Contracts

Immutable typed families cover occurrence, time, status, identity, relationship,
corporate action, filing metadata, earnings schedule, actual, estimate, guidance and
macro value. Predicates are controlled. Subjects use existing event, issuer/security/
listing/graph-node, filing, corporate and relationship IDs. Numeric contexts reuse EIE-5
measurement, reference-period and canonical-unit contracts. Different periods, stages,
units or measurement identities are separate propositions; incompatible numeric units
cannot be silently compared. Normalized quantity comparisons use existing conversions.
Numeric semantics also distinguish actual/prior-period roles, estimate mean/median and
individual contributors. Guidance retains supplied lower/upper bounds or a point; missing
bounds remain missing.

Provider claim keys identify stable records across source items within one configured
provider/resource. Distinct records must have distinct keys, including records in different
items; a later revision retains its record's key. Dataset, provider and resource namespace
those keys. Conflicting inputs for one revision number fail closed, and identical repeated
inputs do not inflate alternatives or corroboration.

`EvidenceReference` pins the raw observation/item, payload hash, fragment hash, parser
and adapter version and an exact field, JSON pointer, CSV row/column, static XML path or
SEC metadata field. Optional byte/Unicode-character spans are half-open and carry an
explicit content length. References contain no raw body or fragment text. One reference
can support multiple assertions; an assertion can link multiple references. Each reference
is stored once in `ce_references`, with links in `ce_assertion_evidence`.

When source bytes are supplied to ingestion, their hash/size and every located fragment
are verified in memory. No raw archive is created by the evidence layer. Without retained
bytes, fragment hashes and precise field locations remain supplied provenance assertions;
validation verifies their linkage rather than claiming independent source authentication.
Native archival remains governed by EIE-6/7 retention and licensing policy.

## Relationships and audit

`SUPPORTS`, `CONTRADICTS`, `CONFIRMS`, `CORRECTS`, `UPDATES`, `RETRACTS`, `SUPERSEDES`,
`DUPLICATE_OF` and `POSSIBLE_DUPLICATE_OF` are distinct directed relationships. A change
points to the same provider thread's immediate predecessor at a later knowledge time.
An explicit correction can reassign a wrong semantic binding while retaining the original.
Independent confirmation requires different configured source families and compatible
values. Automatic symmetric support/conflict links have canonical observation ordering.

Source families identify underlying origins: a company release and its syndicated mirrors
can share a family. Repeated provider delivery is idempotent; later revisions never count
as extra independent sources. Unknown times provide no positive confirmation or conflict.
Descriptive summaries expose source classes, independent support, confirmation, conflict,
correction and retraction counts and latest relevant times. No scalar confidence score.

`EvidenceQueries` supports semantic claims, cutoff-aware histories, references, supporting
assertions, contradictions, field resolutions, source-item audit and event/source audit.
Bounded traversal starts from a raw item, follows its assertions and claim relationships,
and returns reference/claim/link IDs with explicit truncation. All source content is inert.
No NLP, model invocation, executable deserialization or provider-issued tools are involved.


## EIE-12 text extraction

EIE-12 verified candidates produce the existing `Claim`, `ProviderClaimObservation`,
`EvidenceReference` and `EvidenceBatch` contracts. Exact original CHARACTER spans
and fragment hashes connect to segments, normalization maps and documents. Models
remain processing metadata; source independence and canonical field resolution stay
in EIE-11. Accepted links, source checkpoint and extraction artifacts commit atomically.
The internal event-link clock check now uses max(acquisition ingestion, lineage known
availability), retaining existing historical semantics while admitting later processing.

See [structured extraction](structured-extraction.md) and [text ingestion](text-ingestion.md).
