"""Normalized indexed SQLite storage; validation and artifact sealing share a lock."""

from threading import get_ident
from typing import TypeVar

from eie.core.errors import PersistenceError, ValidationError
from eie.core.time import require_ns
from eie.evidence.claims import Claim, ProviderClaimObservation
from eie.evidence.dedup import EventResolutionRun, resolve_events
from eie.evidence.graph import ClaimRelationship, validate_relationship, validate_revision
from eie.evidence.matching import DuplicateCandidate, EventDescriptor, blocks, match_events
from eie.evidence.models import CanonicalClaimResolution
from eie.evidence.references import AcquisitionLineage, EvidenceDataset, EvidenceReference
from eie.evidence.resolution import resolve_claim
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.evidence.models import EvidenceReceipt

T = TypeVar("T")


def typed(payload: object, cls: type[T]) -> T:
    if not isinstance(payload, bytes):
        raise PersistenceError("evidence payload bytes required")
    value = codec.loads(payload)
    if not isinstance(value, cls):
        raise PersistenceError("evidence stored contract type mismatch")
    return value


def limit_check(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100000:
        raise ValidationError("bounded evidence query required")


class EvidenceRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def owned(self) -> None:
        if self.store._depth == 0 or self.store._owner != get_ident():
            raise PersistenceError("evidence mutation requires owned transaction")

    def _put(self, table: str, scalars: tuple[object, ...], value: object) -> bool:
        self.owned()
        desired = (*scalars, codec.dumps(value))
        old = self.store._rows(f"SELECT * FROM {table} WHERE id=?", (scalars[0],))
        if old:
            if old[0] != desired:
                raise PersistenceError("immutable evidence artifact collision")
            return False
        self.store._connection.execute(
            f"INSERT INTO {table} VALUES ({','.join('?' for _ in desired)})", desired
        )
        return True

    def _one(self, table: str, value_id: str, cls: type[T]) -> tuple[T, tuple[object, ...]] | None:
        rows = self.store._rows(f"SELECT * FROM {table} WHERE id=?", (value_id,))
        if not rows:
            return None
        return typed(rows[0][-1], cls), rows[0][:-1]

    def _ids(self, sql: str, parameters: tuple[object, ...], limit: int) -> tuple[str, ...]:
        limit_check(limit)
        rows = self.store._rows(sql + " LIMIT ?", (*parameters, limit + 1))
        if len(rows) > limit:
            raise ValidationError("evidence query result bound exceeded")
        if any(not isinstance(r[0], str) for r in rows):
            raise PersistenceError("invalid evidence reference index")
        return tuple(str(r[0]) for r in rows)

    def knowledge_append(self, dataset_id: str, known_ns: int) -> None:
        self.owned()
        require_ns(known_ns)
        clocks = self.store._rows(
            "SELECT (SELECT MAX(known) FROM ce_receipts WHERE dataset=?),"
            "(SELECT MAX(known) FROM ce_seals WHERE dataset=?)",
            (dataset_id, dataset_id),
        )[0]
        if (
            isinstance(clocks[0], int)
            and known_ns < clocks[0]
            or isinstance(clocks[1], int)
            and known_ns <= clocks[1]
        ):
            raise ValidationError(
                "knowledge must advance beyond sealed cutoffs and cannot backdate completed items"
            )

    def append_dataset(self, value: EvidenceDataset) -> None:
        self.owned()
        if value.build_id is not None:
            from eie.datasets.repository import DatasetRepository

            manifest = DatasetRepository(self.store).manifest(value.build_id)
            if manifest is None or manifest.content_fingerprint != value.fingerprint:
                raise ValidationError(
                    "evidence dataset must pin a completed compatible EIE-7 build"
                )
        self._put("ce_datasets", (value.dataset_id, value.build_id, value.fingerprint), value)

    def dataset(self, value_id: str) -> EvidenceDataset:
        result = self._one("ce_datasets", value_id, EvidenceDataset)
        if result is None:
            raise PersistenceError("missing evidence dataset")
        value, scalars = result
        if scalars != (value.dataset_id, value.build_id, value.fingerprint):
            raise PersistenceError("dataset index mismatch")
        return value

    def append_lineage(self, value: AcquisitionLineage, dataset_id: str) -> None:
        self.owned()
        self.validate_lineage(value, dataset_id)
        self._put(
            "ce_acquisitions",
            (value.lineage_id, value.raw_observation_id, dataset_id, value.known_from_ns),
            value,
        )

    def validate_lineage(self, value: AcquisitionLineage, dataset_id: str) -> None:
        dataset = self.dataset(dataset_id)
        raw = self.store.get_observation(value.raw_observation_id)
        if raw is None or value.build_id != dataset.build_id:
            raise ValidationError("acquisition requires raw observation and exact dataset lineage")
        if value.build_id is not None:
            from eie.datasets.models import HistoricalFact

            rows = self.store._rows(
                "SELECT payload FROM historical_facts WHERE build_id=? AND id=?",
                (value.build_id, value.historical_fact_id),
            )
            if not rows:
                raise ValidationError("historical claim requires retained source-backed EIE-7 fact")
            fact = typed(rows[0][0], HistoricalFact)
            from eie.archive.models import ArchiveAcquisition

            acquired = self.store._rows(
                "SELECT a.payload FROM historical_acquisitions a JOIN historical_lineage l "
                "ON a.id=l.acquisition_id WHERE l.build_id=? AND l.fact_id=? AND a.id=?",
                (value.build_id, value.historical_fact_id, value.historical_acquisition_id),
            )
            if not acquired:
                raise ValidationError(
                    "historical evidence requires exact EIE-7 acquisition lineage"
                )
            acquisition = typed(acquired[0][0], ArchiveAcquisition)
            observed = acquisition.replayed_at_ns or acquisition.acquired_at_ns
            if (
                (
                    fact.raw.observation_id,
                    observed,
                    max(observed, fact.raw.ingested_at_ns),
                    fact.parser_version,
                    fact.quality,
                    fact.source_available_ns,
                )
                != (
                    value.raw_observation_id,
                    value.observed_at_ns,
                    value.ingested_at_ns,
                    value.parser_version,
                    value.reconstruction,
                    value.source_available_ns,
                )
                or fact.fact_id != value.historical_fact_id
                or acquisition.acquisition_id != value.historical_acquisition_id
                or (
                    acquisition.provider_id,
                    acquisition.resource_id,
                    acquisition.parser_version,
                    acquisition.adapter_version,
                )
                != (fact.provider_id, fact.resource_id, fact.parser_version, fact.adapter_version)
            ):
                raise ValidationError("historical acquisition does not match its EIE-7 fact")

    def lineage(self, value_id: str) -> AcquisitionLineage:
        result = self._one("ce_acquisitions", value_id, AcquisitionLineage)
        if result is None:
            raise PersistenceError("missing claim acquisition")
        value, scalars = result
        if (
            len(scalars) != 4
            or scalars[:2] != (value.lineage_id, value.raw_observation_id)
            or scalars[3] != value.known_from_ns
            or not isinstance(scalars[2], str)
        ):
            raise PersistenceError("acquisition index mismatch")
        self.validate_lineage(value, scalars[2])
        return value

    def append_claim(self, value: Claim) -> None:
        self._put(
            "ce_claims",
            (value.claim_id, value.subject.identity, value.predicate.value, value.claim_type.value),
            value,
        )

    def claim(self, value_id: str) -> Claim:
        result = self._one("ce_claims", value_id, Claim)
        if result is None:
            raise PersistenceError("missing semantic claim")
        value, scalars = result
        if scalars != (
            value.claim_id,
            value.subject.identity,
            value.predicate.value,
            value.claim_type.value,
        ):
            raise PersistenceError("semantic claim index mismatch")
        return value

    def append_reference(self, value: EvidenceReference) -> None:
        self.owned()
        self.validate_reference(value)
        self._put(
            "ce_references",
            (value.evidence_id, value.raw_observation_id, value.content_hash, value.parser_version),
            value,
        )

    def validate_reference(self, value: EvidenceReference) -> None:
        raw = self.store.get_observation(value.raw_observation_id)
        if raw is None or (raw.source_item_id, raw.payload_hash, raw.source.adapter_version) != (
            value.source_item_id,
            value.content_hash,
            value.adapter_version,
        ):
            raise ValidationError("evidence reference raw/source/hash/adapter mismatch")

    def reference(self, value_id: str) -> EvidenceReference:
        result = self._one("ce_references", value_id, EvidenceReference)
        if result is None:
            raise PersistenceError("missing evidence reference")
        value, scalars = result
        if scalars != (
            value.evidence_id,
            value.raw_observation_id,
            value.content_hash,
            value.parser_version,
        ):
            raise PersistenceError("evidence reference index mismatch")
        self.validate_reference(value)
        return value

    @staticmethod
    def assertion_scalars(value: ProviderClaimObservation) -> tuple[object, ...]:
        return (
            value.observation_id,
            value.thread_id,
            value.claim_id,
            value.dataset_id,
            value.source.name,
            value.source_family,
            value.resource_id,
            value.raw_observation_id,
            value.lineage_id,
            value.number,
            value.known_from_ns,
            value.valid_from_ns,
            value.valid_to_ns,
            value.event_id,
        )

    def append_assertion(self, value: ProviderClaimObservation) -> None:
        self.owned()
        self.knowledge_append(value.dataset_id, value.known_from_ns)
        old = self.latest_thread(value.thread_id)
        if old != value:
            validate_revision(value, old)
        self.validate_assertion(value)
        if self._put("ce_assertions", self.assertion_scalars(value), value):
            self.store._connection.executemany(
                "INSERT INTO ce_assertion_evidence VALUES (?,?)",
                ((value.observation_id, e) for e in value.evidence_ids),
            )

    def validate_assertion(self, value: ProviderClaimObservation) -> None:
        self.claim(value.claim_id).validate_value(value.value)
        raw = self.store.get_observation(value.raw_observation_id)
        lineage = self.lineage(value.lineage_id)
        rows = self.store._rows(
            "SELECT dataset FROM ce_acquisitions WHERE id=?", (value.lineage_id,)
        )
        if (
            raw is None
            or raw.source != value.source
            or (
                lineage.raw_observation_id,
                lineage.parser_version,
                lineage.known_from_ns,
                rows[0][0],
            )
            != (
                value.raw_observation_id,
                value.parser_version,
                value.known_from_ns,
                value.dataset_id,
            )
        ):
            raise ValidationError("provider assertion source/acquisition/dataset mismatch")
        if (value.provenance, value.reconstruction) != (lineage.provenance, lineage.reconstruction):
            raise ValidationError("provider assertion reconstruction provenance mismatch")
        for evidence_id in value.evidence_ids:
            evidence = self.reference(evidence_id)
            if (evidence.raw_observation_id, evidence.parser_version) != (
                value.raw_observation_id,
                value.parser_version,
            ):
                raise ValidationError("assertion evidence parser/source mismatch")
        if (
            value.event_id is not None
            and self.store.get_event(value.event_id, lineage.ingested_at_ns) is None
        ):
            raise ValidationError("claim-to-event link requires retained known canonical event")

    def assertion(self, value_id: str) -> ProviderClaimObservation:
        result = self._one("ce_assertions", value_id, ProviderClaimObservation)
        if result is None:
            raise PersistenceError("missing provider assertion")
        value, scalars = result
        links = self._ids(
            "SELECT evidence FROM ce_assertion_evidence WHERE assertion=? ORDER BY evidence",
            (value_id,),
            4096,
        )
        if scalars != self.assertion_scalars(value) or links != value.evidence_ids:
            raise PersistenceError("provider assertion scalar/reference index mismatch")
        self.validate_assertion(value)
        return value

    def latest_thread(self, thread_id: str) -> ProviderClaimObservation | None:
        rows = self.store._rows(
            "SELECT id FROM ce_assertions WHERE thread=? ORDER BY number DESC LIMIT 1", (thread_id,)
        )
        return None if not rows else self.assertion(str(rows[0][0]))

    def observations(
        self,
        dataset_id: str,
        known_as_of_ns: int,
        *,
        claim_id: str | None = None,
        event_id: str | None = None,
        raw_id: str | None = None,
        limit: int = 10000,
    ) -> tuple[ProviderClaimObservation, ...]:
        require_ns(known_as_of_ns)
        clauses, args = ["dataset=?", "known<=?"], [dataset_id, known_as_of_ns]
        # Include all revisions of threads that ever asserted this proposition. A
        # correction to a different proposition must retire the prior wrong binding.
        if claim_id is not None:
            clauses.append(
                "thread IN (SELECT thread FROM ce_assertions "
                "WHERE dataset=? AND claim=? AND known<=?)"
            )
            args.extend((dataset_id, claim_id, known_as_of_ns))
        for column, value in (("event", event_id), ("raw", raw_id)):
            if value is not None:
                clauses.append(column + "=?")
                args.append(value)
        ids = self._ids(
            "SELECT id FROM ce_assertions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY known,thread,number,id",
            tuple(args),
            limit,
        )
        return tuple(self.assertion(i) for i in ids)

    def append_relationship(
        self, value: ClaimRelationship, dataset_id: str, maximum: int = 64
    ) -> None:
        self.owned()
        source, target = self.assertion(value.source_id), self.assertion(value.target_id)
        if source.dataset_id != dataset_id:
            raise ValidationError("relationship dataset mismatch")
        validate_relationship(value, source, target)
        old = self._one("ce_relations", value.relationship_id, ClaimRelationship)
        if old is None:
            self.knowledge_append(dataset_id, value.known_from_ns)
        count = self.store._rows(
            "SELECT COUNT(*) FROM ce_relations WHERE source=? OR target=?",
            (value.source_id, value.source_id),
        )[0][0]
        target_count = self.store._rows(
            "SELECT COUNT(*) FROM ce_relations WHERE source=? OR target=?",
            (value.target_id, value.target_id),
        )[0][0]
        if old is None and (
            not isinstance(count, int)
            or not isinstance(target_count, int)
            or max(count, target_count) >= maximum
        ):
            raise ValidationError("relationships per assertion bound exceeded")
        self._put(
            "ce_relations",
            (
                value.relationship_id,
                value.source_id,
                value.target_id,
                dataset_id,
                value.known_from_ns,
            ),
            value,
        )

    def relationship(self, value_id: str) -> ClaimRelationship:
        result = self._one("ce_relations", value_id, ClaimRelationship)
        if result is None:
            raise PersistenceError("missing claim relationship")
        value, scalars = result
        source, target = self.assertion(value.source_id), self.assertion(value.target_id)
        if scalars != (
            value.relationship_id,
            value.source_id,
            value.target_id,
            source.dataset_id,
            value.known_from_ns,
        ):
            raise PersistenceError("claim relationship index mismatch")
        validate_relationship(value, source, target)
        return value

    def relationships(
        self,
        dataset_id: str,
        known_as_of_ns: int,
        assertion_ids: tuple[str, ...],
        limit: int = 10000,
    ) -> tuple[ClaimRelationship, ...]:
        require_ns(known_as_of_ns)
        ids: set[str] = set()
        limit_check(limit)
        if len(assertion_ids) > limit:
            raise ValidationError("bounded relationship input required")
        for assertion_id in assertion_ids:
            ids.update(
                self._ids(
                    "SELECT id FROM ce_relations WHERE dataset=? AND known<=? "
                    "AND (source=? OR target=?) ORDER BY id",
                    (dataset_id, known_as_of_ns, assertion_id, assertion_id),
                    limit,
                )
            )
            if len(ids) > limit:
                raise ValidationError("relationship result bound exceeded")
        return tuple(self.relationship(i) for i in sorted(ids))

    @staticmethod
    def descriptor_scalars(value: EventDescriptor) -> tuple[object, ...]:
        return (
            value.descriptor_id,
            value.event_id,
            value.event_revision,
            value.dataset_id,
            value.thread_id,
            value.raw_observation_id,
            value.number,
            value.known_from_ns,
        )

    def append_descriptor(self, value: EventDescriptor) -> None:
        self.owned()
        self.knowledge_append(value.dataset_id, value.known_from_ns)
        self.validate_descriptor(value)
        prior = self.store._rows(
            "SELECT payload FROM ce_descriptors WHERE thread=? ORDER BY number DESC LIMIT 1",
            (value.thread_id,),
        )
        old = None if not prior else typed(prior[0][0], EventDescriptor)
        if old != value and (
            old is None
            and value.number != 1
            or old is not None
            and (
                value.number != old.number + 1
                or value.previous_id != old.descriptor_id
                or value.known_from_ns <= old.known_from_ns
            )
        ):
            raise ValidationError("event descriptor causal revision chain mismatch")
        if self._put("ce_descriptors", self.descriptor_scalars(value), value):
            self.store._connection.executemany(
                "INSERT INTO ce_blocks VALUES (?,?)",
                ((value.descriptor_id, block) for block in blocks(value)),
            )

    def validate_descriptor(self, value: EventDescriptor) -> None:
        revisions = self.store.get_revisions(value.event_id)
        event = next((r for r in revisions if r.revision.number == value.event_revision), None)
        raw = self.store.get_observation(value.raw_observation_id)
        dataset = self.dataset(value.dataset_id)
        if (
            raw is None
            or raw.source != value.source
            or event is None
            or event.identity.event_type != value.event_type
            or value.raw_observation_id not in event.evidence_observation_ids
            or dataset.build_id is None
            and event.revision.available_from_ns > value.known_from_ns
        ):
            raise ValidationError(
                "event descriptor requires exact source/event/knowledge references"
            )
        if dataset.build_id is None and raw.ingested_at_ns > value.known_from_ns:
            raise ValidationError("descriptor cannot backdate live evidence")

    def descriptor(self, value_id: str) -> EventDescriptor:
        result = self._one("ce_descriptors", value_id, EventDescriptor)
        if result is None:
            raise PersistenceError("missing event descriptor")
        value, scalars = result
        indexed = self._ids(
            "SELECT block FROM ce_blocks WHERE descriptor=? ORDER BY block", (value_id,), 128
        )
        if scalars != self.descriptor_scalars(value) or indexed != blocks(value):
            raise PersistenceError("event descriptor/block index mismatch")
        self.validate_descriptor(value)
        return value

    def descriptors(
        self, dataset_id: str, known_as_of_ns: int, limit: int = 10000
    ) -> tuple[EventDescriptor, ...]:
        require_ns(known_as_of_ns)
        ids = self._ids(
            "SELECT id FROM ce_descriptors WHERE dataset=? AND known<=? "
            "ORDER BY known,thread,number,id",
            (dataset_id, known_as_of_ns),
            limit,
        )
        return tuple(self.descriptor(i) for i in ids)

    def append_resolution(
        self,
        value: CanonicalClaimResolution,
        inputs: tuple[ProviderClaimObservation, ...],
        links: tuple[ClaimRelationship, ...],
    ) -> None:
        self.owned()
        if any(r.known_from_ns > value.known_as_of_ns for r in inputs) or any(
            r.known_from_ns > value.known_as_of_ns for r in links
        ):
            raise ValidationError("claim resolution cannot pin future evidence")
        complete = self.observations(
            value.dataset_id, value.known_as_of_ns, claim_id=value.claim_id, limit=10000
        )
        complete_links = self.relationships(
            value.dataset_id, value.known_as_of_ns, tuple(r.observation_id for r in complete), 10000
        )
        if {r.observation_id for r in complete} != {r.observation_id for r in inputs} or {
            r.relationship_id for r in complete_links
        } != {r.relationship_id for r in links}:
            raise ValidationError("claim resolution must include complete applicable evidence")
        expected = resolve_claim(
            self.claim(value.claim_id),
            value.dataset_id,
            inputs,
            links,
            value.known_as_of_ns,
            value.valid_at_ns,
            value.policy,
        )
        if (
            value != expected
            or any(self.assertion(r.observation_id) != r for r in inputs)
            or any(self.relationship(r.relationship_id) != r for r in links)
        ):
            raise ValidationError("claim resolution must recalculate from exact retained inputs")
        if self._put(
            "ce_claim_resolutions",
            (
                value.resolution_id,
                value.claim_id,
                value.dataset_id,
                value.known_as_of_ns,
                value.valid_at_ns,
                value.state.value,
                value.policy.fingerprint,
            ),
            value,
        ):
            self.store._connection.executemany(
                "INSERT INTO ce_resolution_inputs VALUES (?,?)",
                ((value.resolution_id, r.observation_id) for r in inputs),
            )
            self.store._connection.executemany(
                "INSERT INTO ce_resolution_relations VALUES (?,?)",
                ((value.resolution_id, r.relationship_id) for r in links),
            )

    def stored_resolution(self, value_id: str) -> CanonicalClaimResolution:
        result = self._one("ce_claim_resolutions", value_id, CanonicalClaimResolution)
        if result is None:
            raise PersistenceError("missing canonical claim resolution")
        value, scalars = result
        if scalars != (
            value.resolution_id,
            value.claim_id,
            value.dataset_id,
            value.known_as_of_ns,
            value.valid_at_ns,
            value.state.value,
            value.policy.fingerprint,
        ):
            raise PersistenceError("claim resolution index mismatch")
        inputs = tuple(
            self.assertion(i)
            for i in self._ids(
                "SELECT assertion FROM ce_resolution_inputs WHERE resolution=? ORDER BY assertion",
                (value_id,),
                10000,
            )
        )
        links = tuple(
            self.relationship(i)
            for i in self._ids(
                "SELECT relation FROM ce_resolution_relations WHERE resolution=? ORDER BY relation",
                (value_id,),
                10000,
            )
        )
        expected = resolve_claim(
            self.claim(value.claim_id),
            value.dataset_id,
            inputs,
            links,
            value.known_as_of_ns,
            value.valid_at_ns,
            value.policy,
        )
        if value != expected:
            raise PersistenceError("claim resolution fingerprint/reference consistency mismatch")
        return value

    def candidate(self, value_id: str) -> DuplicateCandidate:
        result = self._one("ce_candidates", value_id, DuplicateCandidate)
        if result is None:
            raise PersistenceError("missing duplicate candidate")
        value, scalars = result
        if scalars != (
            value.candidate_id,
            value.left_id,
            value.right_id,
            value.decision.value,
            value.policy.fingerprint,
        ) or value != match_events(
            self.descriptor(value.left_id), self.descriptor(value.right_id), value.policy
        ):
            raise PersistenceError("duplicate candidate input/index/fingerprint mismatch")
        return value

    def append_event_run(
        self, value: EventResolutionRun, candidates: tuple[DuplicateCandidate, ...]
    ) -> None:
        self.owned()
        inputs = tuple(self.descriptor(i) for i in value.descriptor_ids)
        complete, _ = resolve_events(
            self.descriptors(value.dataset_id, value.known_as_of_ns, value.limits.query_size),
            value.dataset_id,
            value.known_as_of_ns,
            value.policy,
            value.limits,
        )
        if value.descriptor_ids != complete.descriptor_ids:
            raise ValidationError("event resolution must include complete active inputs")
        expected, expected_candidates = resolve_events(
            inputs, value.dataset_id, value.known_as_of_ns, value.policy, value.limits
        )
        if value != expected or candidates != expected_candidates:
            raise ValidationError("event resolution must match exact retained descriptor inputs")
        for c in candidates:
            self._put(
                "ce_candidates",
                (c.candidate_id, c.left_id, c.right_id, c.decision.value, c.policy.fingerprint),
                c,
            )
        if self._put(
            "ce_event_runs",
            (value.run_id, value.dataset_id, value.known_as_of_ns, value.policy.fingerprint),
            value,
        ):
            self.store._connection.executemany(
                "INSERT INTO ce_run_inputs VALUES (?,?)",
                ((value.run_id, i) for i in value.descriptor_ids),
            )
            self.store._connection.executemany(
                "INSERT INTO ce_run_candidates VALUES (?,?)",
                ((value.run_id, i) for i in value.candidate_ids),
            )
            self.store._connection.executemany(
                "INSERT INTO ce_group_members VALUES (?,?,?)",
                ((value.run_id, g.group_id, i) for g in value.groups for i in g.descriptor_ids),
            )

    def stored_event_run(self, value_id: str) -> EventResolutionRun:
        result = self._one("ce_event_runs", value_id, EventResolutionRun)
        if result is None:
            raise PersistenceError("missing event resolution run")
        value, scalars = result
        inputs = self._ids(
            "SELECT descriptor FROM ce_run_inputs WHERE run=? ORDER BY descriptor",
            (value_id,),
            100000,
        )
        candidates = self._ids(
            "SELECT candidate FROM ce_run_candidates WHERE run=? ORDER BY candidate",
            (value_id,),
            100000,
        )
        members = self.store._rows(
            "SELECT group_id,descriptor FROM ce_group_members WHERE run=? "
            "ORDER BY group_id,descriptor",
            (value_id,),
        )
        if (
            scalars
            != (value.run_id, value.dataset_id, value.known_as_of_ns, value.policy.fingerprint)
            or inputs != value.descriptor_ids
            or candidates != value.candidate_ids
            or members != [(g.group_id, d) for g in value.groups for d in g.descriptor_ids]
        ):
            raise PersistenceError("event resolution membership/index mismatch")
        expected, expected_candidates = resolve_events(
            tuple(self.descriptor(i) for i in inputs),
            value.dataset_id,
            value.known_as_of_ns,
            value.policy,
            value.limits,
        )
        if value != expected or tuple(self.candidate(i) for i in candidates) != expected_candidates:
            raise PersistenceError("event resolution recalculation mismatch")
        return value

    def append_receipt(self, value: EvidenceReceipt) -> None:
        self.owned()
        self._put(
            "ce_receipts",
            (
                value.receipt_id,
                value.dataset_id,
                value.provider,
                value.resource_id,
                value.known_from_ns,
                value.generation,
                value.raw_observation_id,
                value.lineage_id,
            ),
            value,
        )

    def receipt(self, value_id: str) -> EvidenceReceipt | None:
        result = self._one("ce_receipts", value_id, EvidenceReceipt)
        if result is None:
            return None
        value, scalars = result
        if scalars != (
            value.receipt_id,
            value.dataset_id,
            value.provider,
            value.resource_id,
            value.known_from_ns,
            value.generation,
            value.raw_observation_id,
            value.lineage_id,
        ):
            raise PersistenceError("evidence receipt/checkpoint index mismatch")
        if (
            self.lineage(value.lineage_id).known_from_ns != value.known_from_ns
            or any(
                self.assertion(i).raw_observation_id != value.raw_observation_id
                for i in value.assertion_ids
            )
            or any(
                self.reference(i).raw_observation_id != value.raw_observation_id
                for i in value.evidence_ids
            )
        ):
            raise PersistenceError("source item receipt references disagree")
        for i in value.relationship_ids:
            self.relationship(i)
        for i in value.resolution_ids:
            self.stored_resolution(i)
        if value.event_run_id is not None:
            self.stored_event_run(value.event_run_id)
        return value

    def generation(self, dataset_id: str, provider: str, resource: str) -> int:
        rows = self.store._rows(
            "SELECT MAX(generation) FROM ce_receipts WHERE dataset=? AND provider=? AND resource=?",
            (dataset_id, provider, resource),
        )
        return int(rows[0][0]) if isinstance(rows[0][0], int) else 0

    def seal(self, dataset_id: str, known_ns: int) -> None:
        self.owned()
        self.store._connection.execute(
            "INSERT OR IGNORE INTO ce_seals VALUES (?,?)", (dataset_id, require_ns(known_ns))
        )
