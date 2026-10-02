"""Atomic raw → evidence → claims → resolutions → source checkpoint ingestion."""

from hashlib import sha256

from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.evidence.claims import ProviderClaimObservation
from eie.evidence.dedup import resolve_events
from eie.evidence.graph import ClaimRelationship
from eie.evidence.matching import DEFAULT_DEDUPE_POLICY, DedupePolicy
from eie.evidence.models import (
    DEFAULT_EVIDENCE_LIMITS,
    DEFAULT_RESOLUTION_POLICY,
    EvidenceLimits,
    ResolutionPolicy,
)
from eie.evidence.queries import EvidenceQueries
from eie.evidence.repository import EvidenceRepository
from eie.evidence.resolution import compatible, current_observations
from eie.evidence.schema import ClaimStatus, EvidenceRelation
from eie.ingestion.models import RetentionPermission
from eie.providers.evidence.models import EvidenceBatch, EvidenceProvider, EvidenceReceipt


class EvidenceEngine:
    def __init__(
        self,
        repository: EvidenceRepository,
        *,
        limits: EvidenceLimits = DEFAULT_EVIDENCE_LIMITS,
        resolution_policy: ResolutionPolicy = DEFAULT_RESOLUTION_POLICY,
        dedupe_policy: DedupePolicy = DEFAULT_DEDUPE_POLICY,
    ) -> None:
        self.repository, self.limits = repository, limits
        self.resolution_policy, self.dedupe_policy = resolution_policy, dedupe_policy

    @property
    def policy_fingerprint(self) -> str:
        return fingerprint(
            "eie:evidence-ingestion-policy:v1",
            (self.limits, self.resolution_policy, self.dedupe_policy),
        )

    def ingest(
        self,
        provider: EvidenceProvider,
        batch: EvidenceBatch,
        *,
        source_payload: bytes | None = None,
    ) -> EvidenceReceipt:
        repo = self.repository
        item_fingerprint = fingerprint("eie:evidence-provider-item:v1", (provider, batch))
        receipt_id = "ereceipt_" + fingerprint(
            "eie:evidence-item-receipt:v1",
            (item_fingerprint, provider.dataset.dataset_id, self.policy_fingerprint),
        )
        with repo.store.transaction():
            if provider.normalized_retention is not RetentionPermission.ALLOWED:
                raise ValidationError("normalized field retention requires configured entitlement")
            if (
                batch.raw.source != provider.source
                or batch.lineage.parser_version != provider.parser_version
                or batch.lineage.build_id != provider.dataset.build_id
            ):
                raise ValidationError("configured evidence source/parser/build disagrees")
            if (
                len(batch.claims) > self.limits.claims_per_item
                or len(batch.references) > self.limits.references_per_item
                or len(batch.relationships)
                > self.limits.claims_per_item * self.limits.relationships_per_claim
            ):
                raise ValidationError("source-item evidence limits exceeded")
            if source_payload is not None and (
                not isinstance(source_payload, bytes)
                or len(source_payload) > provider.archive_policy.max_raw_bytes
                or sha256(source_payload).hexdigest() != batch.raw.payload_hash
            ):
                raise ValidationError("supplied source content hash/byte bound mismatch")
            if source_payload is not None:
                from eie.evidence.content import verify_fragments

                verify_fragments(source_payload, batch.references)
            previous_receipt = repo.receipt(receipt_id)
            if previous_receipt is not None:
                return previous_receipt
            known = batch.lineage.known_from_ns
            repo.append_dataset(provider.dataset)
            repo.knowledge_append(provider.dataset.dataset_id, known)
            old_raw = repo.store.get_observation(batch.raw.observation_id)
            if old_raw is None:
                repo.store.append_observation(batch.raw)
            elif (
                old_raw.source,
                old_raw.source_item_id,
                old_raw.source_item_version,
                old_raw.payload_hash,
                old_raw.content_type,
            ) != (
                batch.raw.source,
                batch.raw.source_item_id,
                batch.raw.source_item_version,
                batch.raw.payload_hash,
                batch.raw.content_type,
            ):
                raise PersistenceError("raw source identity collision")
            elif any(
                getattr(old_raw, name) != getattr(batch.raw, name)
                for name in (
                    "published_at_ns",
                    "source_reference",
                    "raw_event_type",
                    "raw_entity_identifiers",
                    "metadata",
                    "quality_flags",
                )
            ):
                raise PersistenceError("raw source metadata changed without a new item version")
            repo.append_lineage(batch.lineage, provider.dataset.dataset_id)
            for ref in batch.references:
                if (
                    ref.raw_observation_id != batch.raw.observation_id
                    or ref.parser_version != provider.parser_version
                ):
                    raise ValidationError("source item reference parser/raw mismatch")
                repo.append_reference(ref)
            for event in batch.events:
                repo.store.append_event(event)
            assertions: list[ProviderClaimObservation] = []
            changes: list[ClaimRelationship] = []
            affected: set[str] = set()
            refs = {r.evidence_id for r in batch.references}
            for item in batch.claims:
                if (
                    item.claim.claim_type not in provider.capabilities
                    or not set(item.evidence_ids) <= refs
                ):
                    raise ValidationError("unsupported claim family or incomplete item evidence")
                repo.append_claim(item.claim)
                thread_id = "cthread_" + fingerprint(
                    "eie:provider-claim-thread:v1",
                    (
                        provider.dataset.dataset_id,
                        provider.source.name,
                        batch.resource_id,
                        item.provider_claim_key,
                    ),
                )
                prior = repo.latest_thread(thread_id)
                current = ProviderClaimObservation(
                    item.claim.claim_id,
                    provider.source,
                    provider.source_family,
                    provider.dataset.dataset_id,
                    batch.resource_id,
                    item.provider_claim_key,
                    batch.raw.observation_id,
                    batch.lineage.lineage_id,
                    provider.parser_version,
                    1 if prior is None else prior.number + 1,
                    known,
                    item.value,
                    item.evidence_ids,
                    item.status,
                    previous_id=None if prior is None else prior.observation_id,
                    valid_from_ns=item.valid_from_ns,
                    valid_to_ns=item.valid_to_ns,
                    event_id=item.event_id,
                    provenance=batch.lineage.provenance,
                    reconstruction=batch.lineage.reconstruction,
                )
                if prior is not None:
                    relation = (
                        EvidenceRelation.CORRECTS
                        if item.status is ClaimStatus.CORRECTED
                        else EvidenceRelation.RETRACTS
                        if item.status in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN)
                        else EvidenceRelation.UPDATES
                    )
                    changes.append(
                        ClaimRelationship(
                            current.observation_id, prior.observation_id, relation, known
                        )
                    )
                    affected.add(prior.claim_id)
                repo.append_assertion(current)
                assertions.append(current)
                affected.add(current.claim_id)
            for current in assertions:
                others = current_observations(
                    repo.observations(
                        provider.dataset.dataset_id,
                        known,
                        claim_id=current.claim_id,
                        limit=self.limits.query_size,
                    ),
                    known,
                    None,
                )
                for other in others:
                    if (
                        other.observation_id == current.observation_id
                        or other.claim_id != current.claim_id
                        or other.thread_id == current.thread_id
                        or other.status in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN)
                        or current.status in (ClaimStatus.RETRACTED, ClaimStatus.WITHDRAWN)
                    ):
                        continue
                    from eie.evidence.claims import TimePrecision, TimeValue

                    if any(
                        isinstance(v, TimeValue) and v.precision is TimePrecision.UNKNOWN
                        for v in (current.value, other.value)
                    ):
                        continue
                    kind = (
                        EvidenceRelation.CONTRADICTS
                        if not compatible(current.value, other.value)
                        else EvidenceRelation.CONFIRMS
                        if current.source_family != other.source_family
                        else EvidenceRelation.SUPPORTS
                    )
                    a, b = sorted((current.observation_id, other.observation_id))
                    changes.append(
                        ClaimRelationship(
                            a, b, kind, max(current.known_from_ns, other.known_from_ns)
                        )
                    )
            relationships = {r.relationship_id: r for r in (*batch.relationships, *changes)}
            for link in sorted(relationships.values(), key=lambda r: r.relationship_id):
                repo.append_relationship(
                    link, provider.dataset.dataset_id, self.limits.relationships_per_claim
                )
            for descriptor in batch.descriptors:
                if (
                    descriptor.source,
                    descriptor.dataset_id,
                    descriptor.resource_id,
                    descriptor.raw_observation_id,
                    descriptor.known_from_ns,
                ) != (
                    provider.source,
                    provider.dataset.dataset_id,
                    batch.resource_id,
                    batch.raw.observation_id,
                    known,
                ):
                    raise ValidationError("source item event descriptor lineage mismatch")
                repo.append_descriptor(descriptor)
            queries = EvidenceQueries(repo, provider.dataset.dataset_id, self.limits)
            resolutions: list[str] = []
            for claim_id in sorted(affected):
                rows = repo.observations(
                    provider.dataset.dataset_id,
                    known,
                    claim_id=claim_id,
                    limit=self.limits.query_size,
                )
                links = repo.relationships(
                    provider.dataset.dataset_id,
                    known,
                    tuple(r.observation_id for r in rows),
                    self.limits.query_size,
                )
                result = queries.resolution(claim_id, known, policy=self.resolution_policy)
                repo.append_resolution(result, rows, links)
                resolutions.append(result.resolution_id)
            event_run_id = None
            if batch.descriptors:
                run, candidates = resolve_events(
                    repo.descriptors(provider.dataset.dataset_id, known, self.limits.query_size),
                    provider.dataset.dataset_id,
                    known,
                    self.dedupe_policy,
                    self.limits,
                )
                repo.append_event_run(run, candidates)
                event_run_id = run.run_id
            receipt = EvidenceReceipt(
                item_fingerprint,
                provider.dataset.dataset_id,
                provider.source.name,
                batch.resource_id,
                batch.raw.observation_id,
                batch.lineage.lineage_id,
                known,
                repo.generation(
                    provider.dataset.dataset_id, provider.source.name, batch.resource_id
                )
                + 1,
                tuple(sorted(r.observation_id for r in assertions)),
                tuple(sorted(refs)),
                tuple(sorted(relationships)),
                tuple(sorted(resolutions)),
                event_run_id,
                self.policy_fingerprint,
            )
            repo.append_receipt(receipt)
            return receipt

    def freeze(
        self, queries: EvidenceQueries, known_as_of_ns: int, valid_at_ns: int | None = None
    ) -> tuple[tuple[str, ...], str]:
        """Persist fully recalculated views and prevent later additions at their cutoff."""
        repo = self.repository
        if queries.repository is not repo:
            raise ValidationError("freeze requires the same evidence repository")
        with repo.store.transaction():
            ids: list[str] = []
            for result in queries.claims_as_of(known_as_of_ns, valid_at_ns, self.resolution_policy):
                rows = repo.observations(
                    queries.dataset_id,
                    known_as_of_ns,
                    claim_id=result.claim_id,
                    limit=self.limits.query_size,
                )
                links = repo.relationships(
                    queries.dataset_id,
                    known_as_of_ns,
                    tuple(r.observation_id for r in rows),
                    self.limits.query_size,
                )
                repo.append_resolution(result, rows, links)
                ids.append(result.resolution_id)
            run, candidates = resolve_events(
                repo.descriptors(queries.dataset_id, known_as_of_ns, self.limits.query_size),
                queries.dataset_id,
                known_as_of_ns,
                self.dedupe_policy,
                self.limits,
            )
            repo.append_event_run(run, candidates)
            repo.seal(queries.dataset_id, known_as_of_ns)
            return tuple(ids), run.run_id
