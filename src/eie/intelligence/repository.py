"""Immutable indexed outputs, policies, manifests and separate information/audit refs."""

from eie.core.errors import PersistenceError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.time import require_ns
from eie.evidence.claims import NumericRange
from eie.evidence.repository import EvidenceRepository
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import MeasurementContext, ObservationSource
from eie.expectations.settings import ExpectationsSettings
from eie.expectations.surprise import SurpriseObservation, calculate_surprise, with_normalization
from eie.expectations.units import Quantity
from eie.impact.models import EventImpactResult
from eie.impact.repository import ImpactRepository
from eie.intelligence.inputs import IntelligenceInputs
from eie.intelligence.models import (
    EventIntelligenceResult,
    IntelligenceAudit,
    IntelligenceInputManifest,
    IntelligenceRunManifest,
)
from eie.intelligence.policy import (
    DEFAULT_INTELLIGENCE_LIMITS,
    IntelligenceLimits,
    IntelligencePolicy,
)
from eie.intelligence.uncertainty import impact_uncertainty
from eie.persistence.sqlite import SQLiteEventStore

DEFAULT_EXPECTATIONS_SETTINGS = ExpectationsSettings()


class IntelligenceRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store, self.evidence = store, EvidenceRepository(store)

    def append_policy(self, value: IntelligencePolicy) -> str:
        with self.store.transaction():
            self.evidence._put("ei_policies", (value.policy_id, value.fingerprint), value)
        return value.policy_id

    def policy(self, identity: str) -> IntelligencePolicy:
        result = self.evidence._one("ei_policies", identity, IntelligencePolicy)
        if result is None or result[1] != (result[0].policy_id, result[0].fingerprint):
            raise PersistenceError("missing/corrupt intelligence policy")
        return result[0]

    @staticmethod
    def indexes(value: EventIntelligenceResult) -> tuple[object, ...]:
        r = value.manifest.request
        return (
            value.intelligence_id,
            r.event_id,
            r.revision_number,
            r.dataset_id,
            value.manifest.build_id,
            value.event_family,
            value.event_type,
            r.known_as_of_ns,
            r.available_ns,
            r.policy_id,
            value.materiality.category.value,
            value.manifest.manifest_id,
            value.result_fingerprint,
        )

    def _append(self, value: EventIntelligenceResult, data: IntelligenceInputs) -> None:
        self.evidence.owned()
        manifest = value.manifest
        self.evidence._put(
            "ei_inputs",
            (
                manifest.manifest_id,
                manifest.request.dataset_id,
                manifest.request.policy_id,
                manifest.request.known_as_of_ns,
                manifest.input_fingerprint,
            ),
            manifest,
        )
        if self.evidence._put("ei_results", self.indexes(value), value):
            connection = self.store._connection
            connection.executemany(
                "INSERT INTO ei_entities VALUES (?,?)",
                ((value.intelligence_id, i) for i in value.entity_ids),
            )
            connection.executemany(
                "INSERT INTO ei_sources VALUES (?,?)",
                (
                    (value.intelligence_id, i)
                    for i in sorted({f.assertion.source_family for f in data.current})
                ),
            )
            connection.executemany(
                "INSERT INTO ei_measurements VALUES (?,?,?)",
                (
                    (
                        value.intelligence_id,
                        f.claim.measurement.measurement_id.code,
                        f.claim.claim_id,
                    )
                    for f in data.current
                    if f.claim.measurement is not None
                ),
            )
            connection.executemany(
                "INSERT INTO ei_novelty VALUES (?,?,?,?,?)",
                (
                    (
                        value.intelligence_id,
                        c.dimension.value,
                        c.subject_id,
                        c.state.value,
                        c.component_id,
                    )
                    for c in value.novelty
                ),
            )
            connection.executemany(
                "INSERT INTO ei_materiality VALUES (?,?,?,?)",
                (
                    (value.intelligence_id, c.kind.value, c.subject_id, c.category.value)
                    for c in value.materiality.components
                ),
            )
            connection.executemany(
                "INSERT INTO ei_uncertainty VALUES (?,?,?)",
                ((value.intelligence_id, c.kind.value, c.state.value) for c in value.uncertainty),
            )

    def persist_resolutions(self, data: IntelligenceInputs, limits: IntelligenceLimits) -> None:
        request = data.manifest.request
        for fact in data.current:
            rows = self.evidence.observations(
                request.dataset_id,
                request.known_as_of_ns,
                claim_id=fact.claim.claim_id,
                limit=limits.prior_claims,
            )
            links = self.evidence.relationships(
                request.dataset_id,
                request.known_as_of_ns,
                tuple(r.observation_id for r in rows),
                limits.prior_claims,
            )
            self.evidence.append_resolution(fact.resolution, rows, links)

    def append(
        self,
        value: EventIntelligenceResult,
        *,
        limits: IntelligenceLimits = DEFAULT_INTELLIGENCE_LIMITS,
    ) -> None:
        from eie.intelligence.collection import collect
        from eie.intelligence.engine import calculate

        with self.store.transaction():
            policy = self.policy(value.manifest.request.policy_id)
            data = collect(
                self.store, value.manifest.request, policy, limits, select_event_resolution=False
            )
            if calculate(data, policy, limits) != value:
                raise ValidationError("intelligence must equal exact source/policy recalculation")
            self.persist_resolutions(data, limits)
            self._append(value, data)
            self.evidence.seal(value.manifest.request.dataset_id, value.baseline_cutoff_ns)

    def get(self, identity: str) -> EventIntelligenceResult | None:
        with self.store.transaction():
            result = self.evidence._one("ei_results", identity, EventIntelligenceResult)
            if result is None:
                return None
            value, scalars = result
            manifest = value.manifest
            stored = self.evidence._one(
                "ei_inputs", manifest.manifest_id, IntelligenceInputManifest
            )
            if (
                scalars != self.indexes(value)
                or stored
                != (
                    manifest,
                    (
                        manifest.manifest_id,
                        manifest.request.dataset_id,
                        manifest.request.policy_id,
                        manifest.request.known_as_of_ns,
                        manifest.input_fingerprint,
                    ),
                )
                or (self.policy(manifest.request.policy_id).fingerprint != value.policy_fingerprint)
            ):
                raise PersistenceError("intelligence input/policy/result indexes disagree")
            tables: tuple[tuple[str, str, list[tuple[object, ...]]], ...] = (
                ("ei_entities", "entity", [(i,) for i in value.entity_ids]),
                (
                    "ei_novelty",
                    "dimension,subject,state,component",
                    [
                        (c.dimension.value, c.subject_id, c.state.value, c.component_id)
                        for c in value.novelty
                    ],
                ),
                (
                    "ei_materiality",
                    "kind,subject,category",
                    [
                        (c.kind.value, c.subject_id, c.category.value)
                        for c in value.materiality.components
                    ],
                ),
                (
                    "ei_uncertainty",
                    "kind,state",
                    [(c.kind.value, c.state.value) for c in value.uncertainty],
                ),
            )
            for table, columns, expected in tables:
                rows = self.store._rows(
                    f"SELECT {columns} FROM {table} WHERE result=? ORDER BY {columns}", (identity,)
                )
                if rows != sorted(expected):
                    raise PersistenceError("intelligence component membership mismatch")
            source_ids = tuple(r.identity for r in manifest.current if r.kind.value == "ASSERTION")
            claims = tuple(
                self.evidence.claim(self.evidence.assertion(i).claim_id) for i in source_ids
            )
            measurements = sorted(
                {
                    (c.measurement.measurement_id.code, c.claim_id)
                    for c in claims
                    if c.measurement is not None
                }
            )
            if (
                self.store._rows(
                    "SELECT measurement,claim FROM ei_measurements WHERE result=? "
                    "ORDER BY measurement,claim",
                    (identity,),
                )
                != measurements
            ):
                raise PersistenceError("intelligence measurement membership mismatch")
            families = sorted({self.evidence.assertion(i).source_family for i in source_ids})
            if self.store._rows(
                "SELECT family FROM ei_sources WHERE result=? ORDER BY family", (identity,)
            ) != [(f,) for f in families]:
                raise PersistenceError("intelligence source-family membership mismatch")
            return value

    def append_guidance(self, value: GuidanceObservation) -> str:
        identity = "intel_guidance_" + value.result_fingerprint
        with self.store.transaction():
            event_id = self._information_source(value.source, value.context.event_id)
            numeric = (
                value.point
                if value.low is None and value.high is None
                else NumericRange(value.low, value.high, value.point)
            )
            assert numeric is not None
            self._supported_numeric(value.source, value.context, numeric)
            self.evidence._put(
                "ei_information",
                (
                    identity,
                    "GUIDANCE",
                    event_id,
                    value.source.available_from_ns,
                    value.result_fingerprint,
                ),
                value,
            )
        return identity

    def _information_source(self, source: ObservationSource, context_event_id: str) -> str:
        raw = self.store.get_observation(source.source_observation_id)
        if (
            raw is None
            or raw.ingested_at_ns != source.ingested_at_ns
            or raw.source_observed_at_ns != source.provider_observed_at_ns
            or raw.source.name != source.provider_id
            or raw.source_item_id != source.provider_source_item_id
            or raw.source.source_class is not source.source_class
        ):
            raise ValidationError("information source clocks/identity/trust disagree")
        event = self.store.get_event(context_event_id, source.available_from_ns)
        if event is not None and raw.observation_id in event.evidence_observation_ids:
            return event.event_id
        # EIE-12 uses a source-neutral measurement referent and source-local event envelopes.
        ids = self.evidence._ids(
            "SELECT id FROM ce_assertions WHERE raw=? AND known<=? ORDER BY id",
            (raw.observation_id, source.available_from_ns),
            10000,
        )
        events = set()
        for identity in ids:
            row = self.evidence.assertion(identity)
            claim = self.evidence.claim(row.claim_id)
            if (
                row.event_id is not None
                and claim.measurement is not None
                and claim.measurement.event_id == context_event_id
            ):
                events.add(row.event_id)
        if len(events) != 1:
            raise ValidationError("information requires one retained canonical source binding")
        return next(iter(events))

    def _supported_numeric(
        self,
        source: ObservationSource,
        context: MeasurementContext,
        value: Quantity | NumericRange,
    ) -> None:
        ids = self.evidence._ids(
            "SELECT id FROM ce_assertions WHERE raw=? AND known<=? ORDER BY id",
            (source.source_observation_id, source.available_from_ns),
            10000,
        )
        for identity in ids:
            row = self.evidence.assertion(identity)
            claim = self.evidence.claim(row.claim_id)
            if claim.measurement == context and row.value == value:
                return
        raise ValidationError("information numeric fields require retained matching source claims")

    def append_surprise(
        self,
        value: SurpriseObservation,
        *,
        settings: ExpectationsSettings = DEFAULT_EXPECTATIONS_SETTINGS,
    ) -> str:
        recomputed = calculate_surprise(
            value.context,
            value.as_of_ns,
            value.actual,
            value.consensus,
            settings,
            value.configuration_fingerprint,
            claims=value.actual_claims,
            previous=value.previous,
            revised_previous=value.revised_previous,
            cutoff_ns=value.consensus_cutoff_ns,
            release_at_ns=value.release_at_ns,
            original_available_ns=value.original_actual_available_from_ns,
        )
        if value.normalization is not None:
            recomputed = with_normalization(recomputed, value.normalization)
        if recomputed != value:
            raise ValidationError("surprise attachment must match EIE-5 component calculation")
        identity = "intel_surprise_" + value.result_fingerprint
        with self.store.transaction():
            for actual in (
                value.actual_claims
                + value.previous
                + value.revised_previous
                + (() if value.actual is None else (value.actual,))
            ):
                self._information_source(actual.source, actual.context.event_id)
                self._supported_numeric(actual.source, actual.context, actual.value)
            event_id = (
                value.context.event_id
                if value.actual is None
                else self._information_source(value.actual.source, value.actual.context.event_id)
            )
            if self.store.get_event(event_id, value.as_of_ns) is None:
                raise ValidationError("surprise event unavailable")
            self.evidence._put(
                "ei_information",
                (
                    identity,
                    "SURPRISE",
                    event_id,
                    value.as_of_ns,
                    value.result_fingerprint,
                ),
                value,
            )
        return identity

    def attach_impact(
        self, intelligence_id: str, impact_ids: tuple[str, ...], available_ns: int
    ) -> IntelligenceAudit:
        require_ns(available_ns)
        with self.store.transaction():
            result = self.get(intelligence_id)
            impacts = tuple(ImpactRepository(self.store).get(i) for i in impact_ids)
            if result is None or not impacts or any(i is None for i in impacts):
                raise ValidationError("retained intelligence and impact references required")
            values = tuple(i for i in impacts if isinstance(i, EventImpactResult))
            if available_ns < result.manifest.request.available_ns or any(
                i.event_id != result.event_id or i.available_ns > available_ns for i in values
            ):
                raise ValidationError("impact audit event/availability mismatch")
            audit = IntelligenceAudit(
                intelligence_id,
                impact_ids,
                tuple(i.result_fingerprint for i in values),
                impact_uncertainty(values),
                available_ns,
            )
            self.evidence._put(
                "ei_audits",
                (
                    audit.audit_id,
                    intelligence_id,
                    available_ns,
                    fingerprint("eie:intelligence-audit-payload:v1", audit),
                ),
                audit,
            )
            return audit

    def audit(self, identity: str) -> IntelligenceAudit | None:
        result = self.evidence._one("ei_audits", identity, IntelligenceAudit)
        if result is None:
            return None
        value, scalars = result
        if scalars != (
            value.audit_id,
            value.intelligence_id,
            value.available_ns,
            fingerprint("eie:intelligence-audit-payload:v1", value),
        ):
            raise PersistenceError("impact audit indexes disagree")
        expected = self.attach_impact(value.intelligence_id, value.impact_ids, value.available_ns)
        if expected != value:
            raise PersistenceError("impact audit recalculation mismatch")
        return value

    def append_run(self, value: IntelligenceRunManifest) -> None:
        with self.store.transaction():
            results = tuple(self.get(i) for i in value.intelligence_ids)
            if (
                any(
                    r is None
                    or r.manifest.request.dataset_id != value.dataset_id
                    or r.manifest.request.policy_id != value.policy_id
                    or r.manifest.request.available_ns > value.available_ns
                    for r in results
                )
                or tuple(r.result_fingerprint for r in results if r is not None)
                != value.result_fingerprints
            ):
                raise ValidationError("intelligence run references disagree")
            if self.evidence._put(
                "ei_runs",
                (
                    value.run_id,
                    value.dataset_id,
                    value.policy_id,
                    value.available_ns,
                    fingerprint("eie:intelligence-run-payload:v1", value),
                ),
                value,
            ):
                self.store._connection.executemany(
                    "INSERT INTO ei_run_members VALUES (?,?)",
                    ((value.run_id, i) for i in value.intelligence_ids),
                )

    def run(self, identity: str) -> IntelligenceRunManifest | None:
        result = self.evidence._one("ei_runs", identity, IntelligenceRunManifest)
        if result is None:
            return None
        value, scalars = result
        if scalars != (
            value.run_id,
            value.dataset_id,
            value.policy_id,
            value.available_ns,
            fingerprint("eie:intelligence-run-payload:v1", value),
        ) or self.store._rows(
            "SELECT result FROM ei_run_members WHERE run=? ORDER BY result", (identity,)
        ) != [(i,) for i in value.intelligence_ids]:
            raise PersistenceError("intelligence run membership/indexes disagree")
        self.append_run(value)
        return value
