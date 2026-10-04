"""Compact indexed materialization and immutable cutoff seals."""

from hashlib import sha256
from threading import get_ident
from typing import TypeVar, cast

from eie.core.errors import PersistenceError
from eie.core.time import require_ns
from eie.events.scope import EventScope, ScopeKind
from eie.persistence import codec
from eie.persistence.sqlite import SQLiteEventStore
from eie.service import owners
from eie.service.compatibility import check_versions
from eie.service.errors import ServiceError
from eie.service.models import (
    CoverageDeclaration,
    EIESnapshot,
    Publication,
    RetainedExpectation,
    SnapshotRequest,
)
from eie.service.schema import ComponentKind, OwnerKind, ServiceCode

T = TypeVar("T")


def typed(raw: object, cls: type[T]) -> T:
    if not isinstance(raw, bytes) or not isinstance(value := codec.loads(raw), cls):
        raise PersistenceError("service artifact type disagrees")
    return value


class ServiceRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store = store

    @staticmethod
    def indices(p: Publication) -> tuple[object, ...]:
        return (
            p.artifact_id,
            p.catalog_id,
            p.component.value,
            p.reference.kind.value,
            p.reference.owner_id,
            p.group_key,
            p.reference.known_ns,
            p.reference.available_ns,
            p.ordinal,
            p.valid_at_ns,
            p.fingerprint,
        )

    def unsealed(self, catalog: str, available: int) -> None:
        if self.store._rows(
            "SELECT 1 FROM service_seals WHERE catalog=? AND known>=? LIMIT 1", (catalog, available)
        ):
            raise PersistenceError("cannot backdate service publication into a frozen cutoff")

    def publish(
        self, catalog: str, kind: OwnerKind, lookup: tuple[str, ...], available_ns: int
    ) -> Publication:
        require_ns(available_ns)
        with self.store.transaction():
            value = owners.publication(self.store, catalog, kind, lookup, available_ns)
            old = self.store._rows(
                "SELECT id FROM service_publications WHERE id=?", (value.artifact_id,)
            )
            if old:
                if self.publication(value.artifact_id) != value:
                    raise PersistenceError("service publication identity collision")
                return value
            self.unsealed(catalog, available_ns)
            self.store._connection.execute(
                "INSERT INTO service_publications VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (*self.indices(value), codec.dumps(value)),
            )
            self.store._connection.executemany(
                "INSERT INTO service_members VALUES (?,?,?)",
                (
                    (value.artifact_id, s.kind.value, identity)
                    for s in value.scopes
                    for identity in (s.identifiers or ("",))
                ),
            )
            return value

    def retain_expectation(
        self, catalog: str, value: RetainedExpectation, available_ns: int
    ) -> Publication:
        with self.store.transaction():
            self.store._connection.execute(
                "INSERT OR IGNORE INTO service_expectations VALUES (?,?,?)",
                (value.artifact_id, value.fingerprint, codec.dumps(value)),
            )
            return self.publish(catalog, OwnerKind.EXPECTATION, (value.artifact_id,), available_ns)

    def publication(self, identity: str, *, deep: bool = False) -> Publication:
        rows = self.store._rows("SELECT * FROM service_publications WHERE id=?", (identity,))
        if len(rows) != 1:
            raise PersistenceError("missing service publication")
        value = typed(rows[0][-1], Publication)
        if rows[0][:-1] != self.indices(value):
            raise PersistenceError("service publication index/fingerprint disagrees")
        members = self.store._rows(
            "SELECT kind,identifier FROM service_members WHERE publication=? ORDER BY "
            "kind,identifier",
            (identity,),
        )
        expected = sorted((s.kind.value, i) for s in value.scopes for i in (s.identifiers or ("",)))
        if (
            members != expected
            or sha256(
                owners.payload(self.store, value.reference.kind, value.reference.lookup)
            ).hexdigest()
            != value.reference.storage_fingerprint
        ):
            raise PersistenceError("service membership/retained owner bytes disagree")
        if (
            deep
            and owners.publication(
                self.store,
                value.catalog_id,
                value.reference.kind,
                value.reference.lookup,
                value.reference.available_ns,
            )
            != value
        ):
            raise PersistenceError("service publication owner projection disagrees")
        if deep:
            from eie.service.audit import audit

            audit(self.store, owners.read(self.store, value.reference.kind, value.reference.lookup))
        return value

    def declare(self, value: CoverageDeclaration) -> str:
        with self.store.transaction():
            rows = self.store._rows(
                "SELECT payload FROM service_coverage WHERE id=?", (value.artifact_id,)
            )
            if rows:
                if self.declaration(value.artifact_id) != value:
                    raise PersistenceError("coverage declaration collision")
                return value.artifact_id
            self.unsealed(value.catalog_id, value.available_ns)
            self.store._connection.execute(
                "INSERT INTO service_coverage VALUES (?,?,?,?,?,?,?,?)",
                (
                    value.artifact_id,
                    value.catalog_id,
                    value.component.value,
                    value.scope.kind.value,
                    value.scope.identifiers[0] if value.scope.identifiers else "",
                    value.available_ns,
                    value.fingerprint,
                    codec.dumps(value),
                ),
            )
            return value.artifact_id

    def declaration(self, identity: str) -> CoverageDeclaration:
        rows = self.store._rows("SELECT * FROM service_coverage WHERE id=?", (identity,))
        if len(rows) != 1:
            raise PersistenceError("missing coverage declaration")
        v = typed(rows[0][-1], CoverageDeclaration)
        if rows[0][:-1] != (
            v.artifact_id,
            v.catalog_id,
            v.component.value,
            v.scope.kind.value,
            v.scope.identifiers[0] if v.scope.identifiers else "",
            v.available_ns,
            v.fingerprint,
        ):
            raise PersistenceError("coverage declaration index disagrees")
        return v

    def coverage(
        self, request: SnapshotRequest, component: ComponentKind, scopes: tuple[EventScope, ...]
    ) -> CoverageDeclaration | None:
        for scope in (request.scope, *scopes):
            ids = self.store._rows(
                "SELECT id FROM service_coverage WHERE catalog=? AND component=? AND kind=? "
                "AND identifier=? AND available<=? ORDER BY available DESC,id LIMIT 2",
                (
                    request.catalog_id,
                    component.value,
                    scope.kind.value,
                    scope.identifiers[0] if scope.identifiers else "",
                    request.known_as_of_ns,
                ),
            )
            if ids:
                values = tuple(self.declaration(str(row[0])) for row in ids)
                if len(values) > 1 and values[0].available_ns == values[1].available_ns:
                    raise PersistenceError("contradictory same-time coverage declarations")
                return values[0]
        return None

    def collect(
        self,
        request: SnapshotRequest,
        component: ComponentKind,
        scopes: tuple[EventScope, ...],
        *,
        identity_only: bool = False,
    ) -> tuple[Publication, ...]:
        clauses = [
            "p.catalog=?",
            "p.component=?",
            "p.available<=?",
            "(p.valid IS NULL OR p.valid=?)",
        ]
        args: list[object] = [
            request.catalog_id,
            component.value,
            request.known_as_of_ns,
            request.valid_ns,
        ]
        if request.scope.kind is not ScopeKind.MARKET or identity_only:
            clauses.append(
                "EXISTS(SELECT 1 FROM service_members m WHERE m.publication=p.id AND ("
                + " OR ".join("(m.kind=? AND m.identifier=?)" for _ in scopes)
                + "))"
            )
            for s in scopes:
                args.extend((s.kind.value, s.identifiers[0] if s.identifiers else ""))
        clauses.append(
            "NOT EXISTS(SELECT 1 FROM service_publications n WHERE n.catalog=p.catalog "
            "AND n.component=p.component AND n.group_key=p.group_key AND n.available<=? "
            "AND (n.valid IS NULL OR n.valid=?) AND (n.known>p.known OR "
            "(n.known=p.known AND n.ordinal>p.ordinal) OR (n.known=p.known AND "
            "n.ordinal=p.ordinal AND n.available>p.available)))"
        )
        args.extend((request.known_as_of_ns, request.valid_ns))
        maximum = (
            request.limits.max_identity_rows if identity_only else request.limits.max_references
        )
        rows = self.store._rows(
            "SELECT p.id FROM service_publications p WHERE "
            + " AND ".join(clauses)
            + " ORDER BY p.id LIMIT ?",
            (*args, maximum + 1),
        )
        if len(rows) > maximum:
            raise ServiceError(ServiceCode.BOUND_EXCEEDED)
        values = tuple(self.publication(str(r[0])) for r in rows)
        if request.policy_ids and not identity_only:
            values = tuple(
                p
                for p in values
                if not any(v.kind == "POLICY" for v in p.reference.provenance)
                or any(
                    v.kind == "POLICY" and v.identity in request.policy_ids
                    for v in p.reference.provenance
                )
            )
        if request.build_ids and not identity_only:
            values = tuple(
                p
                for p in values
                if not any(v.kind == "BUILD" for v in p.reference.provenance)
                or any(
                    v.kind == "BUILD" and v.identity in request.build_ids
                    for v in p.reference.provenance
                )
            )
        return values

    def append(self, value: EIESnapshot) -> None:
        if not self.store._connection.in_transaction or self.store._owner != get_ident():
            raise PersistenceError("snapshot append requires an owned transaction")
        check_versions(value.versions)
        request = value.request
        self.store._connection.execute(
            "INSERT OR IGNORE INTO service_snapshots VALUES (?,?,?,?,?,?,?,?)",
            (
                value.snapshot_id,
                request.fingerprint,
                request.catalog_id,
                request.known_as_of_ns,
                request.valid_ns,
                value.fingerprint,
                len(codec.dumps(value)),
                codec.dumps(value),
            ),
        )
        if self.snapshot(value.snapshot_id) != value:
            raise PersistenceError("immutable snapshot identity/request collision")
        self.store._connection.execute(
            "INSERT OR IGNORE INTO service_seals VALUES (?,?)",
            (request.catalog_id, request.known_as_of_ns),
        )

    def snapshot(self, identity: str) -> EIESnapshot | None:
        rows = self.store._rows("SELECT * FROM service_snapshots WHERE id=?", (identity,))
        if not rows:
            return None
        value = typed(rows[0][-1], EIESnapshot)
        request = value.request
        if rows[0][:-1] != (
            value.snapshot_id,
            request.fingerprint,
            request.catalog_id,
            request.known_as_of_ns,
            request.valid_ns,
            value.fingerprint,
            len(cast(bytes, rows[0][-1])),
        ):
            raise PersistenceError("snapshot index/bytes/fingerprint disagrees")
        check_versions(value.versions)
        return value

    def cached(self, request: SnapshotRequest) -> EIESnapshot | None:
        ids = self.store._rows(
            "SELECT id FROM service_snapshots WHERE request=?", (request.fingerprint,)
        )
        if not ids:
            return None
        value = self.snapshot(str(ids[0][0]))
        if value is None or value.request != request:
            raise PersistenceError("snapshot request index disagrees")
        return value
