"""Information changes stay separate from source independence and numeric surprise."""

from eie.core.fingerprints import fingerprint
from eie.evidence.claims import ActionValue, IdentityValue, NumericRange, RelationshipValue
from eie.evidence.resolution import compatible
from eie.evidence.schema import ClaimStatus, ClaimType, ResolutionState
from eie.expectations.units import Quantity
from eie.intelligence.inputs import DocumentInput, FactInput, IntelligenceInputs
from eie.intelligence.models import NoveltyComponent
from eie.intelligence.numeric import bounds, delta, metric, numeric_novelty
from eie.intelligence.policy import IntelligencePolicy
from eie.intelligence.schema import NoveltyDimension as D
from eie.intelligence.schema import NoveltyState as S
from eie.sources.trust import SourceClass

PRIMARY_CLASSES = frozenset(
    {
        SourceClass.OFFICIAL_PRIMARY,
        SourceClass.EXCHANGE_PRIMARY,
        SourceClass.COMPANY_PRIMARY,
        SourceClass.REGULATED_FILING,
    }
)


def entity_ids(facts: tuple[FactInput, ...]) -> tuple[str, ...]:
    ids: set[str] = set()
    for f in facts:
        if f.event is not None:
            ids.update(
                i
                for i in f.event.identity.scope.identifiers
                if i.startswith(("iss_", "sec_", "lst_", "nod_", "node_"))
            )
        value = f.assertion.value
        if isinstance(value, IdentityValue):
            ids.add(value.target.identity)
        elif isinstance(value, RelationshipValue):
            ids.update((value.source.identity, value.target.identity))
        elif isinstance(value, ActionValue):
            for name in ("target_issuer_id", "acquirer_issuer_id", "spun_issuer_id"):
                identity = getattr(value.payload, name, None)
                if isinstance(identity, str):
                    ids.add(identity)
    return tuple(sorted(ids))


def slot(f: FactInput, excluded: tuple[str, ...]) -> str:
    c = f.claim
    subject: object = c.subject.identity
    if subject in excluded:
        subject = excluded
    context = c.measurement
    if context is not None:
        subject = (
            c.subject.identity
            if not c.subject.identity.startswith("evt_")
            else (None if f.event is None else f.event.identity.scope)
        )
    return fingerprint(
        "eie:prior-factual-slot:v1",
        (
            c.claim_type,
            c.subject.kind,
            subject,
            c.predicate,
            None
            if context is None
            else (
                context.measurement_id,
                context.reference_period,
                context.release_stage,
                context.seasonal_adjustment,
            ),
            c.unit,
            c.measurement_semantics,
        ),
    )


def previous_facts(f: FactInput, data: IntelligenceInputs) -> tuple[FactInput, ...]:
    key = slot(f, data.manifest.excluded_event_ids)
    candidates = (
        p
        for p in data.prior
        if p.claim.claim_type is f.claim.claim_type
        and p.claim.predicate is f.claim.predicate
        and p.claim.subject.kind is f.claim.subject.kind
        and p.claim.unit == f.claim.unit
        and (
            f.claim.measurement is None
            and p.claim.measurement is None
            or f.claim.measurement is not None
            and p.claim.measurement is not None
            and f.claim.measurement.measurement_id == p.claim.measurement.measurement_id
            and f.claim.measurement.reference_period == p.claim.measurement.reference_period
        )
    )
    return tuple(p for p in candidates if slot(p, data.manifest.excluded_event_ids) == key)


def document_novelty(doc: DocumentInput, prior: tuple[DocumentInput, ...]) -> NoveltyComponent:
    comparable = tuple(
        p for p in prior if p.normalization_fingerprint == doc.normalization_fingerprint
    )
    exact = tuple(
        p
        for p in prior
        if p.original_hash == doc.original_hash
        or p in comparable
        and p.normalized_hash == doc.normalized_hash
    )
    prior_segments = {h for p in comparable for h in p.segment_hashes}
    prior_fields = {h for p in prior for h in p.structured_hashes}
    repeated = sum(h in prior_segments for h in doc.segment_hashes)
    rate = (
        0.0 if exact else (1 - repeated / len(doc.segment_hashes) if doc.segment_hashes else None)
    )
    recurrence = (
        sum(h in prior_fields for h in doc.structured_hashes) / len(doc.structured_hashes)
        if doc.structured_hashes
        else None
    )
    state = S.EXACT_REPEAT if exact else S.PARTIAL_REPEAT if repeated else S.NEW_DOCUMENT
    matches = exact or tuple(
        p for p in comparable if set(p.segment_hashes) & set(doc.segment_hashes)
    )
    reasons = ("NORMALIZATION_POLICY_NOT_COMPARABLE",) if prior and not comparable else ()
    return NoveltyComponent(
        D.DOCUMENT,
        doc.run.document_id,
        state,
        (doc.run.run_id,),
        tuple(sorted({p.run.run_id for p in matches})),
        (
            metric("new_segment_fraction", rate, reason="NO_SEGMENTS"),
            metric("structured_field_recurrence", recurrence, reason="NO_ACCEPTED_FIELDS"),
        ),
        reasons,
    )


def factual_novelty(f: FactInput, previous: tuple[FactInput, ...]) -> NoveltyComponent:
    r = f.assertion
    same = tuple(
        p
        for p in previous
        if p.resolution.value is not None and compatible(r.value, p.resolution.value)
    )
    if r.status in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN):
        state = S.RETRACTION
    elif r.status is ClaimStatus.CORRECTED:
        state = S.CORRECTION
    elif f.resolution.state is not ResolutionState.RESOLVED:
        state = S.UNRESOLVED
    elif r.status is ClaimStatus.UPDATED:
        state = S.RESTATEMENT if same else S.UPDATED_FACT
    elif not previous:
        state = S.NEW_FACT
    elif same:
        state = S.CONFIRMATION if r.status is ClaimStatus.CONFIRMED else S.KNOWN_ALREADY
    else:
        state = S.UPDATED_FACT
    return NoveltyComponent(
        D.FACTUAL,
        f.claim.claim_id,
        state,
        (r.observation_id,),
        tuple(sorted(p.assertion.observation_id for p in previous)),
        reasons=("PRIOR_SLOT_UNRESOLVED",)
        if any(p.resolution.state is ResolutionState.UNRESOLVED for p in previous)
        else (),
    )


def cross_source_novelty(f: FactInput, previous: tuple[FactInput, ...]) -> NoveltyComponent:
    r = f.assertion
    same = tuple(
        p
        for p in previous
        if compatible(p.assertion.value, r.value)
        and p.assertion.status not in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN)
    )
    if r.status is ClaimStatus.CORRECTED:
        state = S.CORRECTION
    elif r.status in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN):
        state = S.NO_NEW_INFORMATION
    elif same:
        state = (
            S.SYNDICATED_REPEAT
            if r.source_family in {p.assertion.source_family for p in same}
            else S.INDEPENDENT_CONFIRMATION
        )
    elif previous or f.resolution.conflicts:
        state = S.CONTRADICTION
    else:
        state = (
            S.NEW_PRIMARY_INFORMATION
            if r.source.source_class in PRIMARY_CLASSES
            else S.NEW_SECONDARY_INFORMATION
        )
    return NoveltyComponent(
        D.CROSS_SOURCE,
        f.claim.claim_id,
        state,
        (r.observation_id,),
        tuple(sorted(p.assertion.observation_id for p in previous)),
        (
            metric(
                "independent_prior_families",
                float(len({p.assertion.source_family for p in previous})),
            ),
        ),
    )


def guidance_novelty(
    f: FactInput,
    previous: tuple[FactInput, ...],
    data: IntelligenceInputs,
    policy: IntelligencePolicy,
) -> NoveltyComponent:
    value = f.assertion.value
    assert isinstance(value, (Quantity, NumericRange))
    context = f.claim.measurement
    assert context is not None
    priors = [
        p
        for p in previous
        if p.resolution.selected_id == p.assertion.observation_id
        and isinstance(p.assertion.value, (Quantity, NumericRange))
    ]
    # EIE-5 company guidance is admissible history, never analyst consensus.
    guides = [
        g
        for g in data.guidance
        if g.source.available_from_ns < data.manifest.request.known_as_of_ns
        and g.context.measurement_id == context.measurement_id
        and g.context.reference_period == context.reference_period
        and g.context.release_stage == context.release_stage
        and g.context.seasonal_adjustment == context.seasonal_adjustment
        and g.unit == value.canonical_unit
        and g.issuer_id in data.event.identity.scope.identifiers
    ]
    candidates = [
        (p.assertion.known_from_ns, p.assertion.observation_id, p.assertion.value) for p in priors
    ]
    candidates.extend(
        (g.source.available_from_ns, g.result_fingerprint, NumericRange(g.low, g.high, g.point))
        for g in guides
    )
    prior = max(candidates, key=lambda p: (p[0], p[1])) if candidates else None
    old = None if prior is None else prior[2]
    assert old is None or isinstance(old, (Quantity, NumericRange))
    low, high, point = bounds(value)
    plow, phigh, ppoint = (None, None, None) if old is None else bounds(old)
    midpoint = low / 2 + high / 2 if low is not None and high is not None else point
    pmidpoint = plow / 2 + phigh / 2 if plow is not None and phigh is not None else ppoint
    width = high - low if low is not None and high is not None else None
    pwidth = phigh - plow if plow is not None and phigh is not None else None
    state = (
        S.WITHDRAWN
        if f.assertion.status in (ClaimStatus.WITHDRAWN, ClaimStatus.RETRACTED)
        else (
            S.UNRESOLVED
            if f.resolution.state is not ResolutionState.RESOLVED
            else S.INITIATED
            if old is None
            else S.KNOWN_ALREADY
            if compatible(value, old)
            else S.CHANGED
        )
    )
    unavailable = state in (S.WITHDRAWN, S.INITIATED, S.UNRESOLVED)
    reason = "GUIDANCE_" + state.value
    relative = (
        None
        if pmidpoint is None or midpoint is None or abs(pmidpoint) <= policy.novelty.near_zero
        else (midpoint - pmidpoint) / abs(pmidpoint)
    )
    metrics = tuple(
        sorted(
            (
                metric(
                    name,
                    None if unavailable else change,
                    unit,
                    reason if unavailable else "MISSING_BOUND",
                )
                for name, change, unit in (
                    ("high_change", delta(high, phigh), value.canonical_unit),
                    ("low_change", delta(low, plow), value.canonical_unit),
                    ("midpoint_change", delta(midpoint, pmidpoint), value.canonical_unit),
                    ("relative_midpoint_change", relative, None),
                    ("width_change", delta(width, pwidth), value.canonical_unit),
                )
            ),
            key=lambda m: m.name,
        )
    )
    if not unavailable and relative is None:
        metrics = tuple(
            metric(m.name, None, reason="NEAR_ZERO_BASELINE")
            if m.name == "relative_midpoint_change"
            and pmidpoint is not None
            and abs(pmidpoint) <= policy.novelty.near_zero
            else m
            for m in metrics
        )
    return NoveltyComponent(
        D.GUIDANCE,
        f.claim.claim_id,
        state,
        (f.assertion.observation_id,),
        () if prior is None else (prior[1],),
        metrics,
        (reason,) if unavailable else (),
    )


def calculate_novelty(
    data: IntelligenceInputs, policy: IntelligencePolicy
) -> tuple[NoveltyComponent, ...]:
    components = [document_novelty(d, data.prior_documents) for d in data.documents]
    for f in data.current:
        previous = previous_facts(f, data)
        components.extend((factual_novelty(f, previous), cross_source_novelty(f, previous)))
        if isinstance(f.assertion.value, (Quantity, NumericRange)):
            if f.claim.claim_type is ClaimType.GUIDANCE_VALUE:
                components.append(guidance_novelty(f, previous, data, policy))
            elif (
                f.resolution.state is ResolutionState.RESOLVED
                and f.resolution.value is not None
                and compatible(f.assertion.value, f.resolution.value)
            ):
                components.append(
                    numeric_novelty(f, data.prior, data.manifest.excluded_event_ids, policy.novelty)
                )
            else:
                components.append(
                    NoveltyComponent(
                        D.NUMERIC,
                        f.claim.claim_id,
                        S.UNRESOLVED,
                        (f.assertion.observation_id,),
                        (),
                        reasons=("CURRENT_NUMERIC_UNRESOLVED",),
                    )
                )
    known = set(entity_ids(data.prior))
    if data.prior_graph is not None:
        known.update(n.node_id for n in data.prior_graph.nodes)
    for identity in entity_ids(data.current):
        components.append(
            NoveltyComponent(
                D.ENTITY,
                identity,
                S.KNOWN_ENTITY if identity in known else S.NEW_ENTITY,
                tuple(
                    sorted(
                        f.assertion.observation_id
                        for f in data.current
                        if identity in entity_ids((f,))
                    )
                ),
                tuple(
                    sorted(
                        {
                            f.assertion.observation_id
                            for f in data.prior
                            if identity in entity_ids((f,))
                        }
                        | (
                            {data.prior_graph.snapshot_id}
                            if data.prior_graph is not None
                            and identity in {n.node_id for n in data.prior_graph.nodes}
                            else set()
                        )
                    )
                ),
                reasons=("KNOWN_IN_PRIOR_GRAPH",)
                if data.prior_graph is not None
                and identity in {n.node_id for n in data.prior_graph.nodes}
                else (),
            )
        )
    for doc in data.documents:
        if doc.unresolved_entity:
            components.append(
                NoveltyComponent(
                    D.ENTITY,
                    doc.run.document_id,
                    S.UNRESOLVED,
                    (doc.run.run_id,),
                    (),
                    reasons=("UNRESOLVED_NAMES_OR_ROLES",),
                )
            )
    for dimension, reason in (
        (D.DOCUMENT, "DOCUMENT_INPUT_MISSING"),
        (D.NUMERIC, "NO_NUMERIC_FIELDS"),
        (D.GUIDANCE, "GUIDANCE_NOT_SUPPLIED"),
        (D.ENTITY, "ENTITY_CONTEXT_MISSING"),
    ):
        if not any(c.dimension is dimension for c in components):
            components.append(
                NoveltyComponent(
                    dimension,
                    data.event.event_id,
                    S.UNRESOLVED if dimension is D.ENTITY else S.NO_BASELINE,
                    (data.event.revision_id,),
                    (),
                    reasons=(reason,),
                )
            )
    return tuple(sorted(components, key=lambda c: (c.dimension.value, c.subject_id)))
