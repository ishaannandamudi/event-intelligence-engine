"""Atomic immutable indexed model/fit, impact, sensitivity and run persistence."""

from eie.core.errors import PersistenceError, ValidationError
from eie.core.time import require_ns
from eie.datasets.repository import typed
from eie.impact.engine import run_manifest
from eie.impact.models import EventImpactResult, ImpactRunManifest
from eie.impact.sensitivity import EventImpactSet
from eie.impact.validation import ImpactValidationStatus, validate_impact, validate_set
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.studies.repository import StudyRepository


class ImpactRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    def append(self, result: EventImpactResult) -> bool:
        if validate_impact(result).status is ImpactValidationStatus.INVALID:
            raise ValidationError("invalid completed impact refused")
        payload = codec.dumps(result)
        if len(payload) > 30_000_000:
            raise ValidationError("bounded retained impact artifact required")
        with self.store.transaction():
            old = self.get(result.impact_id)
            if old is not None:
                if old != result:
                    raise PersistenceError("immutable impact identity collision")
                return False
            StudyRepository(self.store).append(result.inputs.study)
            spec, study = result.model_spec, result.inputs.study
            self.store._connection.execute(
                "INSERT INTO impact_models VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    spec.model_id,
                    spec.study_id,
                    spec.event_build_id,
                    result.event_id,
                    study.market.identity.instrument_id,
                    study.anchor_ns,
                    study.definition.event.identity.event_type.code,
                    spec.model_type.value,
                    spec.input_fingerprint,
                    codec.dumps(spec),
                    codec.dumps(result.fit),
                ),
            )
            self.store._connection.execute(
                "INSERT INTO impact_results VALUES (?,?,?,?,?)",
                (
                    result.impact_id,
                    spec.model_id,
                    result.available_ns,
                    result.result_fingerprint,
                    payload,
                ),
            )
            return True

    def get(self, impact_id: str) -> EventImpactResult | None:
        rows = self.store._rows(
            "SELECT m.id,m.study_id,m.build_id,m.event_id,m.instrument,m.anchor,m.event_type,"
            "m.model_type,m.inputs,m.spec,m.fit,r.id,r.available,r.fingerprint,r.payload "
            "FROM impact_models m JOIN impact_results r ON r.model_id=m.id WHERE r.id=?",
            (impact_id,),
        )
        if not rows:
            return None
        row = rows[0]
        result = typed(row[14], EventImpactResult)
        spec, study = result.model_spec, result.inputs.study
        if (
            row[:14]
            != (
                spec.model_id,
                spec.study_id,
                spec.event_build_id,
                result.event_id,
                study.market.identity.instrument_id,
                study.anchor_ns,
                study.definition.event.identity.event_type.code,
                spec.model_type.value,
                spec.input_fingerprint,
                codec.dumps(spec),
                codec.dumps(result.fit),
                result.impact_id,
                result.available_ns,
                result.result_fingerprint,
            )
            or validate_impact(result).status is ImpactValidationStatus.INVALID
            or StudyRepository(self.store).get(spec.study_id) != study
        ):
            raise PersistenceError("impact indexes/fit/fingerprint/raw study disagree")
        return result

    def impacts_as_of(
        self,
        as_of_ns: int,
        *,
        build_id: str | None = None,
        event_id: str | None = None,
        instrument_id: str | None = None,
        model_type: str | None = None,
    ) -> tuple[EventImpactResult, ...]:
        require_ns(as_of_ns)
        clauses = ["r.available < ?", "m.anchor < ?"]
        values: list[object] = [as_of_ns, as_of_ns]
        for column, value in (
            ("m.build_id", build_id),
            ("m.event_id", event_id),
            ("m.instrument", instrument_id),
            ("m.model_type", model_type),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(value)
        rows = self.store._rows(
            "SELECT r.id FROM impact_models m JOIN impact_results r ON r.model_id=m.id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY m.anchor,r.id",
            tuple(values),
        )
        result = []
        for (identity,) in rows:
            r = self.get(str(identity))
            assert r is not None
            if r.available_ns >= as_of_ns or r.inputs.study.anchor_ns >= as_of_ns:
                raise PersistenceError("impact index leaked future result")
            result.append(r)
        return tuple(result)

    def append_set(self, value: EventImpactSet) -> bool:
        if not validate_set(value):
            raise ValidationError("invalid sensitivity set refused")
        payload = codec.dumps(value)
        if len(payload) > 50_000_000:
            raise ValidationError("bounded retained sensitivity artifact required")
        with self.store.transaction():
            for r in value.results:
                self.append(r)
            old = self.sensitivity(value.set_id)
            if old is not None:
                if old != value:
                    raise PersistenceError("immutable sensitivity identity collision")
                return False
            self.store._connection.execute(
                "INSERT INTO impact_sets VALUES (?,?,?)",
                (value.set_id, value.results[0].model_spec.study_id, payload),
            )
            return True

    def sensitivity(self, set_id: str) -> EventImpactSet | None:
        rows = self.store._rows("SELECT study_id,payload FROM impact_sets WHERE id=?", (set_id,))
        if not rows:
            return None
        result = typed(rows[0][1], EventImpactSet)
        if (
            result.set_id != set_id
            or rows[0][0] != result.results[0].model_spec.study_id
            or not validate_set(result)
            or any(self.get(r.impact_id) != r for r in result.results)
        ):
            raise PersistenceError("sensitivity set membership/calculation disagrees")
        return result

    def append_run(self, results: tuple[EventImpactResult, ...]) -> ImpactRunManifest:
        manifest = run_manifest(results)
        with self.store.transaction():
            for r in results:
                self.append(r)
            old = self.run(manifest.run_id)
            if old is None:
                self.store._connection.execute(
                    "INSERT INTO impact_runs VALUES (?,?,?)",
                    (manifest.run_id, manifest.event_build_id, codec.dumps(manifest)),
                )
            elif old != manifest:
                raise PersistenceError("immutable impact run identity collision")
        return manifest

    def run(self, run_id: str) -> ImpactRunManifest | None:
        rows = self.store._rows("SELECT build_id,payload FROM impact_runs WHERE id=?", (run_id,))
        if not rows:
            return None
        result = typed(rows[0][1], ImpactRunManifest)
        members = tuple(self.get(i) for i in result.impact_ids)
        if (
            result.run_id != run_id
            or rows[0][0] != result.event_build_id
            or any(r is None for r in members)
            or run_manifest(tuple(r for r in members if r is not None)) != result
        ):
            raise PersistenceError("impact run membership/counts disagree")
        return result
