"""Immutable prevalidated feature index and reference-only frozen result persistence."""

from eie.analogues.models import (
    AnalogueLimits,
    AnaloguePolicy,
    AnalogueQuery,
    AnalogueResult,
    AnalogueRunManifest,
    ContextFeature,
    FeatureRow,
    MarketReactionSurprise,
    ResponseObservation,
    ResponseSpec,
)
from eie.core.errors import PersistenceError, ValidationError
from eie.evidence.repository import EvidenceRepository
from eie.impact.repository import ImpactRepository
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.studies.repository import StudyRepository

DEFAULT_LIMITS = AnalogueLimits()


class AnalogueRepository:
    def __init__(self, store: SQLiteEventStore, limits: AnalogueLimits = DEFAULT_LIMITS) -> None:
        self.store, self.limits, self.evidence = store, limits, EvidenceRepository(store)

    def append_policy(self, value: AnaloguePolicy) -> str:
        with self.store.transaction():
            self.evidence._put("analogue_policies", (value.policy_id, value.fingerprint), value)
        return value.policy_id

    def policy(self, identity: str) -> AnaloguePolicy:
        row = self.evidence._one("analogue_policies", identity, AnaloguePolicy)
        if row is None or row[1] != (row[0].policy_id, row[0].fingerprint):
            raise PersistenceError("missing/corrupt analogue policy")
        return row[0]

    @staticmethod
    def feature_indexes(value: FeatureRow) -> tuple[object, ...]:
        return (
            value.row_id,
            value.event_id,
            value.revision,
            value.dataset_id,
            value.build_id,
            value.family,
            value.event_type,
            value.event_available_ns,
            value.feature_cutoff_ns,
            value.available_ns,
            value.policy_id,
            value.intelligence_id,
            value.sector,
            value.industry,
            value.materiality,
            value.fingerprint,
        )

    def _feature(self, value: FeatureRow) -> None:
        self.evidence.owned()
        self._seal_check(
            value.dataset_id,
            value.policy_id,
            value.event_available_ns,
            value.row_id,
            "analogue_features",
        )
        if len(value.values) > self.limits.features or len(value.contexts) > self.limits.contexts:
            raise ValidationError("feature row exceeds configured bounds")
        if self.evidence._put("analogue_features", self.feature_indexes(value), value):
            connection = self.store._connection
            connection.executemany(
                "INSERT INTO analogue_entities VALUES (?,?)",
                ((value.row_id, i) for i in value.entity_ids),
            )
            connection.executemany(
                "INSERT INTO analogue_measurements VALUES (?,?)",
                ((value.row_id, i) for i in value.measurement_ids),
            )
            connection.executemany(
                "INSERT INTO analogue_contexts VALUES (?,?,?,?)",
                ((value.row_id, c.dimension, str(c.value), c.fingerprint) for c in value.contexts),
            )

    def index_features(
        self,
        intelligence_id: str,
        policy_id: str,
        contexts: tuple[ContextFeature, ...] = (),
        graph_id: str | None = None,
    ) -> FeatureRow:
        from eie.analogues.features import build_features

        with self.store.transaction():
            value = build_features(
                self, intelligence_id, self.policy(policy_id), contexts, graph_id
            )
            self._feature(value)
            return value

    def append_feature(self, value: FeatureRow) -> None:
        from eie.analogues.features import build_features

        with self.store.transaction():
            expected = build_features(
                self,
                value.intelligence_id,
                self.policy(value.policy_id),
                tuple(c for c in value.contexts if c.source != value.graph_id),
                value.graph_id,
            )
            if value != expected:
                raise ValidationError("feature rows must equal retained source recalculation")
            self._feature(value)

    def feature(self, identity: str) -> FeatureRow | None:
        row = self.evidence._one("analogue_features", identity, FeatureRow)
        if row is None:
            return None
        value, scalars = row
        if scalars != self.feature_indexes(value) or self.store._rows(
            "SELECT fingerprint FROM ei_results WHERE id=?",
            (value.intelligence_id,),
        ) != [(value.intelligence_fingerprint,)]:
            raise PersistenceError("feature index/source fingerprint disagreement")
        for table, fields, expected in (
            ("analogue_entities", "entity", [(i,) for i in value.entity_ids]),
            ("analogue_measurements", "measurement", [(i,) for i in value.measurement_ids]),
            (
                "analogue_contexts",
                "dimension,category,fingerprint",
                [(c.dimension, str(c.value), c.fingerprint) for c in value.contexts],
            ),
        ):
            if (
                self.store._rows(
                    f"SELECT {fields} FROM {table} WHERE row=? ORDER BY {fields}", (identity,)
                )
                != expected
            ):
                raise PersistenceError("feature memberships disagree")
        return value

    def require_feature(self, identity: str) -> FeatureRow:
        value = self.feature(identity)
        if value is None:
            raise PersistenceError("missing analogue feature row")
        return value

    def _seal_check(self, dataset: str, policy: str, clock: int, identity: str, table: str) -> None:
        if self.store._rows(f"SELECT id FROM {table} WHERE id=?", (identity,)):
            return
        if self.store._rows(
            "SELECT 1 FROM analogue_seals WHERE dataset=? AND policy=? AND known>=? LIMIT 1",
            (dataset, policy, clock),
        ):
            raise ValidationError("cannot backdate index inputs into a frozen analogue pool")

    @staticmethod
    def response_indexes(value: ResponseObservation) -> tuple[object, ...]:
        return (
            value.response_id,
            value.feature_row_id,
            value.event_id,
            value.spec.fingerprint,
            value.spec.metric.value,
            value.study_id,
            value.impact_id,
            value.completed_ns,
            value.available_ns,
            value.fingerprint,
        )

    def index_response(
        self, row_id: str, study_id: str, spec: ResponseSpec, impact_id: str | None = None
    ) -> ResponseObservation:
        from eie.analogues.responses import extract_response
        from eie.datasets.models import semantic_record

        with self.store.transaction():
            row = self.require_feature(row_id)
            study = StudyRepository(self.store).get(study_id)
            impact = None if impact_id is None else ImpactRepository(self.store).get(impact_id)
            if study is None or (impact_id is not None and impact is None):
                raise ValidationError("retained real study/impact required")
            from eie.events.models import CanonicalEvent
            from eie.evidence.repository import typed

            event_rows = self.store._rows(
                "SELECT payload FROM events WHERE id=? AND revision=?", (row.event_id, row.revision)
            )
            if not event_rows or semantic_record(
                typed(event_rows[0][0], CanonicalEvent)
            ) != semantic_record(study.definition.event):
                raise ValidationError("study source event must match exact feature revision")
            value = extract_response(row, study, spec, impact)
            self._seal_check(
                row.dataset_id,
                row.policy_id,
                value.available_ns,
                value.response_id,
                "analogue_responses",
            )
            self.evidence._put("analogue_responses", self.response_indexes(value), value)
            return value

    def response(self, identity: str) -> ResponseObservation | None:
        row = self.evidence._one("analogue_responses", identity, ResponseObservation)
        if row is None:
            return None
        value, scalars = row
        if (
            scalars != self.response_indexes(value)
            or self.store._rows(
                "SELECT fingerprint FROM study_results WHERE id=?", (value.study_id,)
            )
            != [(value.study_fingerprint,)]
            or (
                value.impact_id is not None
                and self.store._rows(
                    "SELECT fingerprint FROM impact_results WHERE id=?", (value.impact_id,)
                )
                != [(value.impact_fingerprint,)]
            )
        ):
            raise PersistenceError("response index/source fingerprint disagreement")
        return value

    def require_response(self, identity: str) -> ResponseObservation:
        value = self.response(identity)
        if value is None:
            raise PersistenceError("missing analogue response")
        return value

    @staticmethod
    def result_indexes(value: AnalogueResult) -> tuple[object, ...]:
        q = value.query
        return (
            value.result_id,
            q.query_id,
            q.target_event_id,
            q.target_revision,
            q.dataset_id,
            q.known_as_of_ns,
            q.available_ns,
            q.policy_id,
            value.fingerprint,
        )

    def _result(self, value: AnalogueResult) -> None:
        q = value.query
        if len(codec.dumps(value)) > self.limits.query_output_bytes or any(
            len(u.reasons) > self.limits.uncertainty_reasons for u in value.uncertainty
        ):
            raise ValidationError("analogue output/reason bound exceeded")
        self.evidence._put(
            "analogue_queries", (q.query_id, q.target_row_id, q.known_as_of_ns, q.fingerprint), q
        )
        if self.evidence._put("analogue_results", self.result_indexes(value), value):
            self.store._connection.executemany(
                "INSERT INTO analogue_members VALUES (?,?,?,?,?)",
                (
                    (
                        value.result_id,
                        m.row_id,
                        m.response_id,
                        None if m.exclusion is None else m.exclusion.value,
                        m.row_fingerprint,
                    )
                    for m in value.pool
                ),
            )
            self.store._connection.executemany(
                "INSERT INTO analogue_selected VALUES (?,?,?,?)",
                (
                    (value.result_id, c.row_id, c.total_distance, c.fingerprint)
                    for c in value.candidates
                ),
            )
            self.store._connection.executemany(
                "INSERT INTO analogue_uncertainty VALUES (?,?,?)",
                ((value.result_id, u.kind.value, u.state.value) for u in value.uncertainty),
            )
        self.store._connection.execute(
            "INSERT OR IGNORE INTO analogue_seals VALUES (?,?,?,?)",
            (q.dataset_id, q.policy_id, q.known_as_of_ns, q.query_id),
        )

    def append_result(self, value: AnalogueResult) -> None:
        from eie.analogues.pool import collect
        from eie.analogues.retrieval import retrieve

        with self.store.transaction():
            policy = self.policy(value.query.policy_id)
            if retrieve(collect(self, value.query, policy), value.query, policy) != value:
                raise ValidationError("analogue result requires exact pool/distance recalculation")
            self._result(value)

    def get(self, identity: str) -> AnalogueResult | None:
        with self.store.transaction():
            row = self.evidence._one("analogue_results", identity, AnalogueResult)
            if row is None:
                return None
            value, scalars = row
            q = value.query
            if len(codec.dumps(value)) > self.limits.query_output_bytes:
                raise ValidationError("analogue query output bound exceeded")
            if (
                scalars != self.result_indexes(value)
                or self.policy(q.policy_id).fingerprint != value.policy_fingerprint
                or self.require_feature(q.target_row_id).fingerprint != value.target_fingerprint
                or self.evidence._one("analogue_queries", q.query_id, AnalogueQuery)
                != (q, (q.query_id, q.target_row_id, q.known_as_of_ns, q.fingerprint))
            ):
                raise PersistenceError("frozen query/result references disagree")
            expected = [
                (
                    m.row_id,
                    m.response_id,
                    None if m.exclusion is None else m.exclusion.value,
                    m.row_fingerprint,
                )
                for m in value.pool
            ]
            if (
                self.store._rows(
                    (
                        "SELECT row,response,exclusion,fingerprint FROM analogue_members "
                        "WHERE result=? ORDER BY row"
                    ),
                    (identity,),
                )
                != expected
            ):
                raise PersistenceError("frozen pool memberships disagree")
            for m in value.pool:
                if self.require_feature(m.row_id).fingerprint != m.row_fingerprint or (
                    m.response_id is not None
                    and self.require_response(m.response_id).fingerprint != m.response_fingerprint
                ):
                    raise PersistenceError("frozen source input fingerprint disagreement")
            if self.store._rows(
                (
                    "SELECT row,distance,fingerprint FROM analogue_selected WHERE result=? ORDER"
                    " BY distance,row"
                ),
                (identity,),
            ) != sorted(
                [(c.row_id, c.total_distance, c.fingerprint) for c in value.candidates],
                key=lambda x: (x[1], x[0]),
            ):
                raise PersistenceError("selected analogue memberships disagree")
            if self.store._rows(
                "SELECT kind,state FROM analogue_uncertainty WHERE result=? ORDER BY kind",
                (identity,),
            ) != [(u.kind.value, u.state.value) for u in value.uncertainty]:
                raise PersistenceError("uncertainty memberships disagree")
            return value

    def attach_reaction(
        self, identity: str, target_response_id: str | None, available_ns: int
    ) -> MarketReactionSurprise:
        from eie.analogues.reaction_surprise import compare

        with self.store.transaction():
            frozen = self.get(identity)
            if frozen is None:
                raise ValidationError("reaction comparison requires a frozen retained analogue set")
            target = (
                None if target_response_id is None else self.require_response(target_response_id)
            )
            value = compare(frozen, target, available_ns)
            self.evidence._put(
                "analogue_reactions",
                (value.reaction_id, identity, target_response_id, available_ns, value.fingerprint),
                value,
            )
            return value

    def reaction(self, identity: str) -> MarketReactionSurprise | None:
        from eie.analogues.reaction_surprise import compare

        with self.store.transaction():
            row = self.evidence._one("analogue_reactions", identity, MarketReactionSurprise)
            if row is None:
                return None
            value, scalars = row
            frozen = self.get(value.analogue_result_id)
            target = (
                None
                if value.target_response_id is None
                else self.require_response(value.target_response_id)
            )
            if (
                scalars
                != (
                    value.reaction_id,
                    value.analogue_result_id,
                    value.target_response_id,
                    value.available_ns,
                    value.fingerprint,
                )
                or frozen is None
                or compare(frozen, target, value.available_ns) != value
            ):
                raise PersistenceError("reaction comparison references/calculation disagree")
            return value

    def append_run(self, value: AnalogueRunManifest) -> None:
        with self.store.transaction():
            for identity, fp in zip(value.result_ids, value.result_fingerprints, strict=True):
                result = self.get(identity)
                if (
                    result is None
                    or result.fingerprint != fp
                    or result.query.dataset_id != value.dataset_id
                    or result.query.available_ns > value.available_ns
                ):
                    raise ValidationError("run members must pin available results")
            if self.evidence._put(
                "analogue_runs",
                (value.run_id, value.dataset_id, value.available_ns, value.fingerprint),
                value,
            ):
                self.store._connection.executemany(
                    "INSERT INTO analogue_run_members VALUES (?,?)",
                    ((value.run_id, i) for i in value.result_ids),
                )

    def run(self, identity: str) -> AnalogueRunManifest | None:
        row = self.evidence._one("analogue_runs", identity, AnalogueRunManifest)
        if row is None:
            return None
        value, scalars = row
        if scalars != (
            value.run_id,
            value.dataset_id,
            value.available_ns,
            value.fingerprint,
        ) or self.store._rows(
            "SELECT result FROM analogue_run_members WHERE run=? ORDER BY result", (identity,)
        ) != [(i,) for i in value.result_ids]:
            raise PersistenceError("analogue run indexes disagree")
        for i, fp in zip(value.result_ids, value.result_fingerprints, strict=True):
            r = self.get(i)
            if (
                r is None
                or r.fingerprint != fp
                or r.query.dataset_id != value.dataset_id
                or r.query.available_ns > value.available_ns
            ):
                raise PersistenceError("analogue run references disagree")
        return value
