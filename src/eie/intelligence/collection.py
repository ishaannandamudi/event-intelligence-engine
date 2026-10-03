"""Consistent SQLite snapshot joins to EIE-11/12/10, with no text interpretation."""

from dataclasses import replace

from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.entities.protocols import ResolutionState as EntityState
from eie.events.models import CanonicalEvent
from eie.evidence.claims import ProviderClaimObservation
from eie.evidence.graph import ClaimRelationship
from eie.evidence.models import CanonicalClaimResolution
from eie.evidence.repository import EvidenceRepository
from eie.evidence.resolution import current_observations, resolve_claim
from eie.evidence.schema import DedupeDecision
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.units import Dimension, Quantity
from eie.graph.repository import GraphRepository
from eie.impact.repository import ImpactRepository
from eie.intelligence.inputs import DocumentInput, FactInput, IntelligenceInputs
from eie.intelligence.models import InputReference, IntelligenceInputManifest, IntelligenceRequest
from eie.intelligence.policy import IntelligenceLimits, IntelligencePolicy
from eie.intelligence.schema import ReferenceKind as K
from eie.nlp.mentions import EntityMention
from eie.nlp.models import ExtractedEventCandidate, ExtractionRun
from eie.nlp.repository import TextRepository
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore


def reference(
    kind: K, identity: str, value: object, known_ns: int, digest: str | None = None
) -> InputReference:
    return InputReference(
        kind, identity, digest or fingerprint("eie:intelligence-reference:v1", value), known_ns
    )


def ordered_refs(refs: list[InputReference]) -> tuple[InputReference, ...]:
    unique = {(r.kind.value, r.identity): r for r in refs}
    if len(unique) != len({(r.kind.value, r.identity, r.fingerprint) for r in refs}):
        raise PersistenceError("inconsistent input reference fingerprints")
    return tuple(unique[k] for k in sorted(unique))


def document_input(
    repo: TextRepository, run: ExtractionRun, events: tuple[str, ...] | None
) -> DocumentInput:
    mapping = repo.mapping(run.map_id)
    selected = {a.candidate_id for a in run.accepted if events is None or a.event_id in events}
    candidates = tuple(
        c for i in sorted(selected) if isinstance((c := repo.artifact(i)), ExtractedEventCandidate)
    )
    if len(candidates) != len(selected):
        raise PersistenceError("accepted extraction candidate type mismatch")
    hashes = tuple(
        sorted(
            {
                fingerprint(
                    "eie:document-structured-fields:v1",
                    (
                        c.proposal.family,
                        c.proposal.epistemic,
                        c.proposal.entity_ids,
                        tuple(
                            (n.name, n.quantity, n.upper, n.bound, n.qualifiers)
                            for n in c.proposal.numerics
                        ),
                        tuple((t.name, t.value) for t in c.proposal.text_fields),
                        None if c.proposal.temporal is None else c.proposal.temporal.value,
                        None if c.proposal.period is None else c.proposal.period.period,
                    ),
                )
                for c in candidates
            }
        )
    )
    mentions = tuple(repo.artifact(i) for i in run.entity_mention_ids)
    unresolved = any(
        isinstance(m, EntityMention)
        and m.state is not EntityState.RESOLVED
        and any(
            m.span.start < c.proposal.span.end and m.span.end > c.proposal.span.start
            for c in candidates
        )
        for m in mentions
    )
    return DocumentInput(
        run,
        mapping.original_hash,
        mapping.normalized_hash,
        fingerprint(
            "eie:intelligence-normalization:v1", (mapping.settings.version, mapping.format)
        ),
        tuple(repo.segment(i).content_hash for i in run.segment_ids),
        hashes,
        candidates,
        unresolved,
    )


def collect(
    store: SQLiteEventStore,
    request: IntelligenceRequest,
    policy: IntelligencePolicy,
    limits: IntelligenceLimits,
    *,
    select_event_resolution: bool = True,
) -> IntelligenceInputs:
    evidence = EvidenceRepository(store)
    evidence.owned()
    dataset = evidence.dataset(request.dataset_id)
    cutoff = request.known_as_of_ns
    historical_events: list[CanonicalEvent] = []
    if dataset.build_id is not None:
        from eie.datasets.queries import DatasetReader
        from eie.datasets.repository import DatasetRepository

        reader = DatasetReader(DatasetRepository(store), dataset.build_id)
        request = replace(
            request, available_ns=max(request.available_ns, reader.manifest.completed_at_ns)
        )
        for historical in reader.records(cutoff, kind="CanonicalEvent"):
            if (
                isinstance(historical.record, CanonicalEvent)
                and historical.source_available_ns is not None
            ):
                historical_events.append(
                    replace(
                        historical.record,
                        revision=replace(
                            historical.record.revision,
                            available_from_ns=historical.source_available_ns,
                        ),
                        source_observed_at_ns=None,
                        ingested_at_ns=None,
                        last_updated_at_ns=None,
                    )
                )
    candidates = tuple(
        e
        for e in (*historical_events, *store.get_revisions(request.event_id))
        if e.event_id == request.event_id
        and e.revision.number == request.revision_number
        and e.revision.available_from_ns <= cutoff
    )
    event = (
        min(candidates, key=lambda e: (e.revision.available_from_ns, e.result_fingerprint))
        if candidates
        else None
    )
    if event is None:
        raise ValidationError("exact event revision unavailable at intelligence cutoff")
    current_refs = [reference(K.EVENT, event.revision_id, event, event.revision.available_from_ns)]
    prior_refs: list[InputReference] = []
    context_refs: list[InputReference] = []
    excluded: tuple[str, ...] = (event.event_id,)
    identity_reasons: set[str] = set()
    run_id = request.event_resolution_id
    if run_id is None and select_event_resolution:
        run_rows = store._rows(
            (
                "SELECT id FROM ce_event_runs WHERE dataset=? AND known<=? ORDER BY known "
                "DESC,id DESC LIMIT 1"
            ),
            (request.dataset_id, cutoff),
        )
        run_id = str(run_rows[0][0]) if run_rows else None
        request = replace(request, event_resolution_id=run_id)
    if run_id is not None:
        event_run = evidence.stored_event_run(run_id)
        if event_run.dataset_id != request.dataset_id or event_run.known_as_of_ns > cutoff:
            raise ValidationError("event identity run outside intelligence snapshot")
        context_refs.append(
            reference(K.EVENT_RESOLUTION, event_run.run_id, event_run, event_run.known_as_of_ns)
        )
        group = next((g for g in event_run.groups if event.event_id in g.member_event_ids), None)
        if group is not None:
            excluded = group.member_event_ids
            identity_reasons.add("EIE11_RESOLVED_GROUP")
        if event.event_id in event_run.truncated_event_ids:
            identity_reasons.add("IDENTITY_TRUNCATED")
        descriptors = {
            d.descriptor_id: d for d in (evidence.descriptor(i) for i in event_run.descriptor_ids)
        }
        for candidate_id in event_run.candidate_ids:
            candidate = evidence.candidate(candidate_id)
            if event.event_id in (
                descriptors[candidate.left_id].event_id,
                descriptors[candidate.right_id].event_id,
            ):
                identity_reasons.add("IDENTITY_" + candidate.decision.value)
                if any(r.value == "COARSE_TIME" for r in candidate.matches + candidate.conflicts):
                    identity_reasons.add("COARSE_TIME")
                if candidate.decision is DedupeDecision.UNRESOLVED:
                    identity_reasons.add("IDENTITY_UNRESOLVED")
    ids = evidence._ids(
        "SELECT id FROM ce_assertions WHERE dataset=? AND known<=? ORDER BY id",
        (request.dataset_id, cutoff),
        limits.prior_claims,
    )
    rows = tuple(evidence.assertion(i) for i in ids)
    active = current_observations(rows, cutoff, None)
    focus = tuple(r for r in active if r.event_id == event.event_id)
    if not focus:
        raise ValidationError("event requires sourced EIE-11 claims in selected dataset")
    current_by_claim: dict[str, ProviderClaimObservation] = {}
    for row in sorted(focus, key=lambda r: (r.known_from_ns, r.observation_id)):
        current_by_claim[row.claim_id] = row
    if len(current_by_claim) > limits.current_claims:
        raise ValidationError("current claim bound exceeded")
    current_ids = {r.observation_id for r in current_by_claim.values()}
    prior_rows = tuple(
        r for r in rows if r.known_from_ns < cutoff and r.observation_id not in current_ids
    )
    link_ids = evidence._ids(
        "SELECT id FROM ce_relations WHERE dataset=? AND known<=? ORDER BY id",
        (request.dataset_id, cutoff),
        limits.prior_claims,
    )
    links = tuple(evidence.relationship(i) for i in link_ids)
    assertion_lookup = {r.observation_id: r for r in rows}
    for link in links:
        if link.known_from_ns == cutoff and not any(
            i in assertion_lookup and assertion_lookup[i].claim_id in current_by_claim
            for i in (link.source_id, link.target_id)
        ):
            continue
        context_refs.append(reference(K.RELATION, link.relationship_id, link, link.known_from_ns))
    claims = {i: evidence.claim(i) for i in sorted({r.claim_id for r in rows})}

    def resolutions(
        source: tuple[ProviderClaimObservation, ...], known: int
    ) -> dict[str, CanonicalClaimResolution]:
        by_thread: dict[str, list[ProviderClaimObservation]] = {}
        threads: dict[str, set[str]] = {}
        by_assertion = {r.observation_id: r for r in source}
        related: dict[str, list[ClaimRelationship]] = {}
        for r in source:
            by_thread.setdefault(r.thread_id, []).append(r)
            threads.setdefault(r.claim_id, set()).add(r.thread_id)
        for link in links:
            if link.known_from_ns <= known:
                for assertion_id in (link.source_id, link.target_id):
                    row = by_assertion.get(assertion_id)
                    if row is not None:
                        related.setdefault(row.claim_id, []).append(link)
        result: dict[str, CanonicalClaimResolution] = {}
        for claim_id in sorted(threads):
            members = tuple(
                sorted(
                    (r for thread in threads[claim_id] for r in by_thread[thread]),
                    key=lambda r: r.observation_id,
                )
            )
            if (
                sum(r.claim_id == claim_id for r in current_observations(members, known, None))
                > limits.source_alternatives
            ):
                raise ValidationError("source alternative bound exceeded")
            rels = tuple(
                sorted(
                    {link.relationship_id: link for link in related.get(claim_id, [])}.values(),
                    key=lambda link: link.relationship_id,
                )
            )
            result[claim_id] = resolve_claim(
                claims[claim_id],
                request.dataset_id,
                members,
                rels,
                known,
                policy=policy.novelty.resolution,
            )
        return result

    current_resolutions = resolutions(rows, cutoff)
    prior_resolutions = resolutions(prior_rows, max(0, cutoff - 1))
    for claim_id in current_by_claim:
        resolution = current_resolutions[claim_id]
        current_refs.append(reference(K.RESOLUTION, resolution.resolution_id, resolution, cutoff))
    events: dict[tuple[str, int], object] = {}

    def fact(row: ProviderClaimObservation, resolution: CanonicalClaimResolution) -> FactInput:
        linked = None
        if row.event_id is not None:
            key = row.event_id, row.known_from_ns
            if key not in events:
                retained = store.get_event(row.event_id, row.known_from_ns)
                historical = tuple(
                    e
                    for e in historical_events
                    if e.event_id == row.event_id
                    and e.revision.available_from_ns <= row.known_from_ns
                )
                events[key] = retained or (
                    max(historical, key=lambda e: e.revision.number) if historical else None
                )
            value = events[key]
            if isinstance(value, CanonicalEvent):
                linked = value
        return FactInput(claims[row.claim_id], row, linked, resolution)

    current = tuple(
        fact(current_by_claim[i], current_resolutions[i]) for i in sorted(current_by_claim)
    )
    prior = tuple(
        fact(r, prior_resolutions[r.claim_id])
        for r in current_observations(prior_rows, max(0, cutoff - 1), None)
    )
    for row in rows:
        if (
            row.known_from_ns == cutoff
            and row.claim_id not in current_by_claim
            and row.event_id != event.event_id
        ):
            continue
        ref = reference(K.ASSERTION, row.observation_id, row, row.known_from_ns)
        (
            current_refs
            if row.observation_id in current_ids
            else prior_refs
            if row.known_from_ns < cutoff
            else context_refs
        ).append(ref)
    for f in prior:
        prior_refs.append(
            reference(
                K.PRIOR_RESOLUTION, f.resolution.resolution_id, f.resolution, max(0, cutoff - 1)
            )
        )
        if f.event is not None:
            prior_refs.append(
                reference(K.EVENT, f.event.revision_id, f.event, f.event.revision.available_from_ns)
            )
    text = TextRepository(store)
    run_ids = evidence._ids(
        (
            "SELECT r.id FROM te_runs r JOIN te_documents d ON d.id=r.document WHERE "
            "d.dataset=? AND r.known<=? ORDER BY r.id"
        ),
        (request.dataset_id, cutoff),
        limits.extraction_runs,
    )
    runs = tuple(text.run(i) for i in run_ids)
    latest_docs: dict[str, ExtractionRun] = {}
    for run in sorted(runs, key=lambda r: (r.available_ns, r.run_id)):
        doc = text.document(run.document_id)
        if (
            any(a.event_id == event.event_id for a in run.accepted)
            or doc.raw.observation_id in event.evidence_observation_ids
        ):
            latest_docs[run.document_id] = run
    current_runs = tuple(latest_docs[k] for k in sorted(latest_docs))
    chosen = {r.run_id for r in current_runs}
    prior_runs = tuple(r for r in runs if r.available_ns < cutoff and r.run_id not in chosen)
    documents = tuple(document_input(text, r, (event.event_id,)) for r in current_runs)
    prior_documents = tuple(document_input(text, r, None) for r in prior_runs)
    for run in (*current_runs, *prior_runs):
        (current_refs if run.run_id in chosen else prior_refs).append(
            reference(K.EXTRACTION, run.run_id, run, run.available_ns)
        )
    graph_repo = GraphRepository(store)

    def graph(identity: str | None, strictly_prior: bool) -> object:
        if identity is None:
            return None
        result = graph_repo.snapshot(identity)
        if (
            result is None
            or result.known_as_of_ns > cutoff
            or strictly_prior
            and result.known_as_of_ns >= cutoff
        ):
            raise ValidationError("graph snapshot violates intelligence knowledge cutoff")
        if len(result.edges) > limits.graph_edges:
            raise ValidationError("graph context edge bound exceeded")
        (prior_refs if strictly_prior else context_refs).append(
            reference(K.GRAPH, identity, result, result.known_as_of_ns)
        )
        return result

    from eie.graph.models import GraphSnapshot

    prior_graph = graph(request.prior_graph_id, True)
    current_graph = graph(request.current_graph_id, False)
    assert prior_graph is None or isinstance(prior_graph, GraphSnapshot)
    assert current_graph is None or isinstance(current_graph, GraphSnapshot)
    scale = None
    if request.scale_assertion_id is not None:
        row = evidence.assertion(request.scale_assertion_id)
        c = evidence.claim(row.claim_id)
        if (
            row.dataset_id != request.dataset_id
            or row.known_from_ns > cutoff
            or (
                c.subject.identity not in event.identity.scope.identifiers
                or c.measurement is None
                or c.measurement.measurement_id.code not in policy.materiality.scale_measurements
                or not isinstance(row.value, Quantity)
                or row.value.canonical_unit.dimension is not Dimension.CURRENCY
                or current_resolutions.get(c.claim_id) is None
                or current_resolutions[c.claim_id].selected_id != row.observation_id
            )
        ):
            raise ValidationError(
                "company scale requires an available resolved compatible sourced field"
            )
        scale = fact(row, current_resolutions[c.claim_id])
        context_refs.append(reference(K.ASSERTION, row.observation_id, row, row.known_from_ns))
    guidance: list[GuidanceObservation] = []
    for identity, kind in (
        *((i, K.GUIDANCE) for i in request.guidance_ids),
        *((i, K.SURPRISE) for i in request.surprise_ids),
    ):
        rows_info = store._rows(
            "SELECT kind,event,known,fingerprint,payload FROM ei_information WHERE id=?",
            (identity,),
        )
        if not rows_info:
            raise PersistenceError("missing information attachment")
        name, event_id, known, digest, payload = rows_info[0]
        value = codec.loads(payload) if isinstance(payload, bytes) else None
        if (
            name != kind.value
            or not isinstance(known, int)
            or known > cutoff
            or not isinstance(digest, str)
        ):
            raise ValidationError("information attachment outside intelligence cutoff")
        if kind is K.GUIDANCE and isinstance(value, GuidanceObservation):
            if (
                known != value.source.available_from_ns
                or identity != "intel_guidance_" + value.result_fingerprint
            ):
                raise PersistenceError("guidance identity/availability indexes disagree")
            guidance.append(value)
        elif kind is K.SURPRISE:
            from eie.expectations.surprise import SurpriseObservation

            if not isinstance(value, SurpriseObservation) or event_id != event.event_id:
                raise ValidationError("surprise reference type/event mismatch")
            if known != value.as_of_ns or identity != "intel_surprise_" + value.result_fingerprint:
                raise PersistenceError("surprise identity/availability indexes disagree")
        else:
            raise PersistenceError("information attachment type mismatch")
        if getattr(value, "result_fingerprint", None) != digest:
            raise PersistenceError("information attachment fingerprint mismatch")
        context_refs.append(reference(kind, identity, value, known, digest))
    impacts = tuple(ImpactRepository(store).get(i) for i in request.impact_ids)
    if any(i is None for i in impacts):
        raise PersistenceError("missing impact attachment")
    from eie.impact.models import EventImpactResult

    impact_values = tuple(i for i in impacts if isinstance(i, EventImpactResult))
    for impact in impact_values:
        if impact.event_id != event.event_id or impact.available_ns > cutoff:
            raise ValidationError(
                "ex-ante impact reference must already be available for this event"
            )
        context_refs.append(reference(K.IMPACT, impact.impact_id, impact, impact.available_ns))
    manifest = IntelligenceInputManifest(
        request,
        dataset.fingerprint,
        dataset.build_id,
        ordered_refs(current_refs),
        ordered_refs(prior_refs),
        ordered_refs(context_refs),
        excluded,
    )
    return IntelligenceInputs(
        manifest,
        event,
        current,
        prior,
        documents,
        prior_documents,
        prior_graph,
        current_graph,
        tuple(guidance),
        scale,
        tuple(sorted(identity_reasons)),
        impact_values,
    )
