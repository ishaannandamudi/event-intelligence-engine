# Intelligence replay, persistence and validation

## Explicit clocks and lineage

An `IntelligenceRequest` pins the exact event revision, evidence dataset, policy,
knowledge cutoff, result availability and optional event-resolution run, prior/current
graph snapshots, scale assertion, company guidance, surprise and impact references.
`IntelligenceInputManifest` contains canonical sorted IDs/fingerprints/knowledge clocks,
dataset/build fingerprint, alias exclusions and schema/engine versions. Full source
bodies, claim payloads and extraction audits remain in their owning repositories.

Current/context inputs must be available by `T`; prior references must be strictly
before `T`. A prior graph snapshot must be strictly prior; current context may be
known at `T`. Same-time unrelated events are excluded from the baseline and input
fingerprint. Selection, calculation, EIE-11 resolutions, append and cutoff sealing
occur in one owned SQLite transaction, so concurrent event writes cannot change
the snapshot halfway through calculation. Complete the ingest wave before sealing
its clock. Later backdated evidence inserts into a sealed dataset are refused.

Historical EIE-7 build-bound source availability can precede local acquisition.
The intelligence retains that qualified historical cutoff and becomes locally
available only after the build is completed. Queries require both the baseline
cutoff and actual result availability to be no later than the query clock.
Historical text retains EIE-12 acquisition/extraction availability qualifications.

Future corrections, retractions, extraction reprocessing, graph disclosures and
EIE-11 merges/splits produce new referenced lineage. Existing result bytes remain
unchanged. Replay pins the selected event-resolution run, including its absence;
it does not select a later run while validating an old result. A policy/version
change produces a new policy/input/result identity. Operational size limits are
separate from mathematical policy fingerprints.

## Repository and queries

```python
from eie.intelligence.engine import IntelligenceEngine
from eie.intelligence.models import IntelligenceRequest
from eie.intelligence.policy import IntelligencePolicy
from eie.intelligence.repository import IntelligenceRepository
from eie.intelligence.queries import IntelligenceQueries

repo = IntelligenceRepository(store)
policy = IntelligencePolicy()
repo.append_policy(policy)
result = IntelligenceEngine(repo).compute(
    IntelligenceRequest(event_id, revision, dataset_id, cutoff_ns, available_ns, policy.policy_id)
)
history = IntelligenceQueries(repo, dataset_id).history(
    available_ns, measurement="EARNINGS.REVENUE"
)
```

The additive eighth SQLite migration raises the store to `1.7`. Indexed tables
retain policies, inputs, results, entity/source/measurement memberships, novelty,
materiality, uncertainty, separate information artifacts, impact audits and run
manifests. All EIE-13 tables refuse UPDATE and DELETE. Older migrations and public
schemas are preserved. Failed migrations/calculations roll back atomically.

`history` filters event, family/type, entity, measurement, source family, policy,
materiality, uncertainty state and time. `intelligence_as_of`, `novelty`,
`materiality`, `uncertainty` and `prior_distribution` expose bounded projections.
Ties at a cutoff sort by content ID; use history's policy filter when choosing
among intentional policy versions. Reads verify allowlisted typed payloads,
fingerprints, scalar indexes, memberships and retained policy/source references.
`validate` recollects pinned causal inputs and recalculates the full result,
returning `VALID`, `WARN` for recorded input qualifications, or `INVALID` for
missing/corrupt/inconsistent artifacts. Run manifests pin exact result fingerprints.

## Separate information and impact

`append_guidance` registers an existing EIE-5 company-guidance contract with
matching retained source fields. `append_surprise` verifies the existing EIE-5
calculation and actual-source numeric fields. Source-neutral EIE-12 measurement
referents require a unique retained canonical source binding. These are explicit
references, not a new consensus engine.

An already available same-event EIE-9 impact can be explicitly attached to an
`EX_POST_AUDIT` result. A later impact uses `attach_impact` to create an immutable
`IntelligenceAudit`, with actual EIE-9 IDs, fingerprints and model diagnostics.
The original `EX_ANTE_INTELLIGENCE` stays unchanged. No synthetic impact scalar or
post-event data enters a novelty baseline or materiality calculation.

## Bounds, security and cost

Prior/current claims, source alternatives, extraction runs, graph edges, component
counts, reason counts and query size are bounded and fail closed. Text/model data
remains inert. Only accepted EIE-12 candidates contribute structured source fields;
no eval, exec, pickle, data-driven imports, network transport or source-requested
filesystem action is introduced.

The implementation materializes and deeply validates bounded evidence history for
each requested cutoff. It is proportional to retained history, not constant time.
Input manifests are reference-only but can still be large. The offline profile
`benchmarks/event_intelligence.py` measures 10,000 persisted historical events,
ten families, real EIE-12 document joins, separate component timings, indexes,
storage and byte-identical restart. Profiling and duplicate staged-equality checks
are included in reported costs; there is no CI timing threshold.
