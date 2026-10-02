"""Append-only indexed definitions/results; atomic run persistence outside live caches."""

from eie.core.errors import PersistenceError, ValidationError
from eie.core.time import require_ns
from eie.datasets.models import HistoricalFact, semantic_record
from eie.datasets.repository import typed
from eie.events.models import CanonicalEvent
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.studies.engine import run_manifest
from eie.studies.models import EventStudyResult, StudyRunManifest
from eie.studies.validation import StudyValidationStatus, validate_study


class StudyRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def append(self, result: EventStudyResult) -> bool:
        if validate_study(result).status is StudyValidationStatus.INVALID:
            raise ValidationError("invalid completed study refused")
        payload = codec.dumps(result)
        if len(payload) > 20_000_000:
            raise ValidationError("bounded retained study inputs required")
        with self.store.transaction():
            existing = self.get(result.study_id)
            if existing is not None:
                if existing != result:
                    raise PersistenceError("immutable study identity collision")
                return False
            d = result.definition
            rows = self.store._rows(
                "SELECT payload FROM dataset_manifests WHERE build_id=?", (d.event_build_id,)
            )
            from eie.datasets.models import DatasetManifest

            if (
                not rows
                or typed(rows[0][0], DatasetManifest).content_fingerprint
                != d.event_dataset_fingerprint
            ):
                raise PersistenceError("study event dataset is absent or incompatible")
            facts = tuple(
                typed(row[0], HistoricalFact)
                for row in self.store._rows(
                    "SELECT payload FROM historical_facts WHERE build_id=? AND logical_id=? "
                    "AND kind='CanonicalEvent' AND available<=? "
                    "ORDER BY revision DESC,available DESC,id",
                    (d.event_build_id, d.event.event_id, d.selection_as_of_ns),
                )
            )
            chosen = next((f for f in facts if f.fact_id == d.historical_fact_id), None)
            if (
                chosen is None
                or chosen.quality is not d.reconstruction_quality
                or not isinstance(chosen.record, CanonicalEvent)
                or semantic_record(chosen.record) != semantic_record(d.event)
                or chosen.source_available_ns != d.event.revision.available_from_ns
                or any(
                    f.provider_id == chosen.provider_id
                    and f.source_revision > chosen.source_revision
                    for f in facts
                )
            ):
                raise PersistenceError(
                    "study event fact/revision/quality disagrees with historical build"
                )
            self.store._connection.execute(
                "INSERT INTO study_definitions VALUES (?,?,?,?,?,?,?,?)",
                (
                    result.study_id,
                    d.event.event_id,
                    result.market.identity.instrument_id,
                    result.anchor_ns,
                    d.event.identity.event_type.code,
                    d.event_build_id,
                    d.policy.policy_id,
                    codec.dumps(d),
                ),
            )
            self.store._connection.execute(
                "INSERT INTO study_results VALUES (?,?,?,?)",
                (result.study_id, result.available_ns, result.result_fingerprint, payload),
            )
            return True

    def get(self, study_id: str) -> EventStudyResult | None:
        rows = self.store._rows(
            "SELECT d.id,d.event_id,d.instrument,d.anchor,d.event_type,d.build"
            "_id,d.policy,d.payload,r.available,r.fingerprint,r.payload FROM s"
            "tudy_definitions d JOIN study_results r ON r.id=d.id WHERE d.id=?",
            (study_id,),
        )
        if not rows:
            return None
        row = rows[0]
        result = typed(row[10], EventStudyResult)
        d = result.definition
        if (
            row[:7]
            != (
                result.study_id,
                d.event.event_id,
                result.market.identity.instrument_id,
                result.anchor_ns,
                d.event.identity.event_type.code,
                d.event_build_id,
                d.policy.policy_id,
            )
            or row[7] != codec.dumps(d)
            or row[8:10] != (result.available_ns, result.result_fingerprint)
            or validate_study(result).status is StudyValidationStatus.INVALID
        ):
            raise PersistenceError("study indexes/fingerprint/calculation disagree")
        return result

    def studies_as_of(
        self,
        as_of_ns: int,
        *,
        build_id: str | None = None,
        event_id: str | None = None,
        instrument_id: str | None = None,
        event_type: str | None = None,
        policy_id: str | None = None,
    ) -> tuple[EventStudyResult, ...]:
        require_ns(as_of_ns)
        where = ["r.available < ?", "d.anchor < ?"]
        parameters: list[object] = [as_of_ns, as_of_ns]
        for column, value in (
            ("d.build_id", build_id),
            ("d.event_id", event_id),
            ("d.instrument", instrument_id),
            ("d.event_type", event_type),
            ("d.policy", policy_id),
        ):
            if value is not None:
                where.append(f"{column} = ?")
                parameters.append(value)
        ids = self.store._rows(
            "SELECT d.id FROM study_definitions d JOIN study_results r ON r.id=d.id WHERE "
            + " AND ".join(where)
            + " ORDER BY d.anchor,d.id",
            tuple(parameters),
        )
        result = []
        for (study_id,) in ids:
            r = self.get(str(study_id))
            assert r is not None
            if r.available_ns >= as_of_ns or r.anchor_ns >= as_of_ns:
                raise PersistenceError("study index leaked future result")
            result.append(r)
        return tuple(result)

    def append_run(self, results: tuple[EventStudyResult, ...]) -> StudyRunManifest:
        manifest = run_manifest(results)
        with self.store.transaction():
            for r in results:
                self.append(r)
            old = self.run(manifest.run_id)
            if old is None:
                self.store._connection.execute(
                    "INSERT INTO study_runs VALUES (?,?,?)",
                    (manifest.run_id, manifest.event_build_id, codec.dumps(manifest)),
                )
            elif old != manifest:
                raise PersistenceError("immutable study run collision")
        return manifest

    def run(self, run_id: str) -> StudyRunManifest | None:
        rows = self.store._rows("SELECT build_id,payload FROM study_runs WHERE id=?", (run_id,))
        if not rows:
            return None
        manifest = typed(rows[0][1], StudyRunManifest)
        values = tuple(self.get(i) for i in manifest.study_ids)
        if (
            rows[0][0] != manifest.event_build_id
            or any(r is None for r in values)
            or run_manifest(tuple(r for r in values if r is not None)) != manifest
            or manifest.run_id != run_id
        ):
            raise PersistenceError("study run content/index disagrees")
        return manifest
