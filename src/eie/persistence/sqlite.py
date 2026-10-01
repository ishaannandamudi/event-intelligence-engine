"""Durable event protocol and atomic domain-session/checkpoint commits."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import RLock, get_ident
from typing import TypeVar, cast

from eie.core.errors import PersistenceError, TemporalConsistencyError, ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.serialization import canonical_json_bytes
from eie.core.time import require_ns
from eie.corporate.models import CorporateEvent
from eie.corporate.queries import CorporateCoverage
from eie.entities.models import Issuer, Listing, Security
from eie.entities.sec import (
    SecFilerRevision,
    SecIssuerRevision,
    SecListingRevision,
    SecTickerMapRevision,
)
from eie.events.models import CanonicalEvent
from eie.events.scope import ApplicabilityQuery
from eie.expectations.guidance import GuidanceObservation
from eie.expectations.models import EventReference
from eie.expectations.observations import ActualObservation, EstimateObservation
from eie.expectations.queries import EstimateCoverage
from eie.filings.models import FilingMetadata
from eie.ingestion.models import Checkpoint, Quarantine, RetentionMode, RetentionPolicy
from eie.persistence import codec, sessions
from eie.persistence.memory import InMemoryEventStore
from eie.persistence.migrations import migrate
from eie.persistence.sessions import Session
from eie.persistence.validation import validate_event_append
from eie.scheduled.coverage import CoverageRecord
from eie.scheduled.models import ScheduleRevision
from eie.sources.models import RawSourceObservation

T = TypeVar("T")
DOMAIN_TYPES = (
    CorporateEvent,
    CorporateCoverage,
    Issuer,
    Listing,
    Security,
    SecFilerRevision,
    SecIssuerRevision,
    SecListingRevision,
    SecTickerMapRevision,
    EventReference,
    ActualObservation,
    EstimateObservation,
    GuidanceObservation,
    EstimateCoverage,
    FilingMetadata,
    CoverageRecord,
    ScheduleRevision,
)


def _typed(payload: bytes, cls: type[T]) -> T:
    value = codec.loads(payload)
    if not isinstance(value, cls):
        raise PersistenceError("stored contract type mismatch")
    return value


def _checked_event(row: tuple[object, ...]) -> CanonicalEvent:
    event = _typed(cast(bytes, row[3]), CanonicalEvent)
    if (event.event_id, event.revision.number, event.revision.available_from_ns) != row[:3]:
        raise PersistenceError("event index disagrees with stored causal contract")
    return event


class SQLiteEventStore:
    def __init__(self, path: str | Path, *, busy_timeout_ms: int = 5000) -> None:
        if (
            not str(path)
            or "\x00" in str(path)
            or str(path).startswith("file:")
            or (str(path) != ":memory:" and Path(path).is_dir())
        ):
            raise ValidationError("trusted SQLite file path required")
        if (
            isinstance(busy_timeout_ms, bool)
            or not isinstance(busy_timeout_ms, int)
            or not 1 <= busy_timeout_ms <= 60_000
        ):
            raise ValidationError("bounded SQLite busy timeout required")
        self._lock = RLock()
        self._depth = 0
        self._owner: int | None = None
        self._closed = False
        try:
            self._connection = sqlite3.connect(
                str(path),
                isolation_level=None,
                check_same_thread=False,
                timeout=busy_timeout_ms / 1000,
            )
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            migrate(self._connection)
        except (sqlite3.Error, PersistenceError) as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise PersistenceError("cannot open SQLite store") from exc

    def _check(self) -> None:
        if self._closed:
            raise PersistenceError("SQLite store is closed")

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._check()
            level = self._depth
            savepoint = f"eie_{level}"
            try:
                self._connection.execute(
                    "BEGIN IMMEDIATE" if level == 0 else f"SAVEPOINT {savepoint}"
                )
                self._depth += 1
                self._owner = get_ident()
                yield
                self._connection.execute(
                    "COMMIT" if level == 0 else f"RELEASE SAVEPOINT {savepoint}"
                )
            except BaseException as exc:
                if self._connection.in_transaction:
                    self._connection.execute(
                        "ROLLBACK" if level == 0 else f"ROLLBACK TO SAVEPOINT {savepoint}"
                    )
                    if level:
                        self._connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                if isinstance(exc, sqlite3.Error):
                    raise PersistenceError("SQLite transaction failed") from exc
                raise
            finally:
                self._depth = level
                if level == 0:
                    self._owner = None

    def _rows(
        self, statement: str, parameters: tuple[object, ...] = ()
    ) -> list[tuple[object, ...]]:
        with self._lock:
            self._check()
            try:
                return list(self._connection.execute(statement, parameters))
            except sqlite3.Error as exc:
                raise PersistenceError("SQLite read failed") from exc

    def append_observation(self, observation: RawSourceObservation) -> bool:
        with self.transaction():
            previous = self.get_observation(observation.observation_id)
            if previous is not None:
                if previous != observation:
                    raise PersistenceError(
                        "observation identity collision or inconsistent re-ingestion"
                    )
                return False
            self._connection.execute(
                "INSERT INTO observations VALUES (?,?,?,?,?)",
                (
                    observation.observation_id,
                    observation.source.name,
                    observation.source_item_id,
                    observation.ingested_at_ns,
                    codec.dumps(observation),
                ),
            )
            return True

    def get_observation(self, observation_id: str) -> RawSourceObservation | None:
        rows = self._rows("SELECT payload FROM observations WHERE id=?", (observation_id,))
        if not rows:
            return None
        value = _typed(cast(bytes, rows[0][0]), RawSourceObservation)
        if value.observation_id != observation_id:
            raise PersistenceError("observation index disagrees with stored identity")
        return value

    def append_event(self, event: CanonicalEvent) -> bool:
        with self.transaction():
            if not validate_event_append(self, event):
                return False
            self._connection.execute(
                "INSERT INTO events VALUES (?,?,?,?)",
                (
                    event.event_id,
                    event.revision.number,
                    event.revision.available_from_ns,
                    codec.dumps(event),
                ),
            )
            self._connection.executemany(
                "INSERT INTO evidence VALUES (?,?,?)",
                (
                    (event.event_id, event.revision.number, identity)
                    for identity in event.evidence_observation_ids
                ),
            )
            return True

    def get_event(self, event_id: str, as_of_ns: int) -> CanonicalEvent | None:
        require_ns(as_of_ns)
        rows = self._rows(
            "SELECT id,revision,available,payload FROM events WHERE id=? AND available<=? "
            "ORDER BY revision DESC LIMIT 1",
            (event_id, as_of_ns),
        )
        return None if not rows else _checked_event(rows[0])

    def get_revisions(self, event_id: str) -> tuple[CanonicalEvent, ...]:
        return tuple(
            _checked_event(r)
            for r in self._rows(
                "SELECT id,revision,available,payload FROM events WHERE id=? ORDER BY revision",
                (event_id,),
            )
        )

    def known_as_of(
        self,
        as_of_ns: int,
        query: ApplicabilityQuery | None = None,
        start_ns: int | None = None,
        end_ns: int | None = None,
    ) -> tuple[CanonicalEvent, ...]:
        require_ns(as_of_ns)
        # Filtering delegates to the same domain policy after historical resolution.
        memory = InMemoryEventStore()
        rows = self._rows(
            "SELECT e.id,e.revision,e.available,e.payload FROM events e JOIN "
            "(SELECT id,MAX(revision) revision FROM "
            "events WHERE available<=? GROUP BY id) v ON e.id=v.id AND "
            "e.revision=v.revision ORDER BY e.id",
            (as_of_ns,),
        )
        memory._events = {v.event_id: [v] for r in rows for v in (_checked_event(r),)}
        return memory.known_as_of(as_of_ns, query, start_ns, end_ns)

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(
            {
                "observations": [
                    _typed(cast(bytes, r[0]), RawSourceObservation)
                    for r in self._rows("SELECT payload FROM observations ORDER BY id")
                ],
                "events": [
                    _typed(cast(bytes, r[0]), CanonicalEvent)
                    for r in self._rows("SELECT payload FROM events ORDER BY id,revision")
                ],
            }
        )

    def save_session(self, namespace: str, session: Session) -> None:
        from eie.ingestion.models import label

        label(namespace)
        values = list(sessions.contracts(session))
        with self.transaction():
            for value in values:
                if isinstance(value, RawSourceObservation):
                    self.append_observation(value)
            pending = [v for v in values if isinstance(v, CanonicalEvent)]
            while pending:
                ready = [
                    v
                    for v in pending
                    if v.revision.supersedes_event_id is None
                    or self.get_event(v.revision.supersedes_event_id, v.revision.available_from_ns)
                    is not None
                ]
                if not ready:
                    raise PersistenceError("unresolved durable supersession")
                for value in sorted(
                    ready,
                    key=lambda v: (v.revision.available_from_ns, v.event_id, v.revision.number),
                ):
                    self.append_event(value)
                completed_ids = {v.revision_id for v in ready}
                pending = [v for v in pending if v.revision_id not in completed_ids]
            for value in values:
                if isinstance(value, DOMAIN_TYPES):
                    self._save_contract(value)
            self._connection.execute(
                "INSERT INTO sessions VALUES (?,?,?) ON CONFLICT(namespace) DO UPDATE SET "
                "layout_version=excluded.layout_version,payload=excluded.payload",
                (namespace, sessions.LAYOUT_VERSION, sessions.dumps(session)),
            )

    def _save_contract(self, value: object) -> None:
        key = fingerprint(
            "eie:durable-contract:v1", (f"{type(value).__module__}.{type(value).__name__}", value)
        )
        payload = codec.dumps(value)
        existing = self._connection.execute(
            "SELECT payload FROM domain_records WHERE id=?", (key,)
        ).fetchone()
        if existing is not None:
            if bytes(existing[0]) != payload:
                raise PersistenceError("durable contract collision")
            return
        context = getattr(value, "context", None)
        revision = getattr(value, "revision", None)
        available = getattr(
            value, "available_from_ns", getattr(revision, "available_from_ns", None)
        )
        if isinstance(value, ScheduleRevision):
            available = value.event.revision.available_from_ns
        event_id = getattr(context, "event_id", getattr(value, "event_id", None))
        if isinstance(value, ScheduleRevision):
            event_id = value.event.event_id
        measurement = getattr(getattr(context, "measurement_id", None), "code", None)
        issuer = getattr(
            value,
            "issuer_id",
            getattr(
                context, "issuer_id", getattr(getattr(value, "identity", None), "issuer_id", None)
            ),
        )
        accession = getattr(getattr(value, "accession", None), "formatted", None)
        self._connection.execute(
            "INSERT INTO domain_records VALUES (?,?,?,?,?,?,?,?)",
            (
                key,
                type(value).__name__,
                available,
                issuer,
                event_id,
                measurement,
                accession,
                payload,
            ),
        )
        evidence: tuple[str, ...] = ()
        if isinstance(value, (EstimateObservation, ActualObservation, GuidanceObservation)):
            evidence = (value.source.source_observation_id,)
        elif isinstance(value, (CorporateEvent, ScheduleRevision)):
            evidence = tuple(v.observation_id for v in value.claims)
        elif isinstance(value, FilingMetadata):
            evidence = (value.source_observation_id,)
        self._connection.executemany(
            "INSERT OR IGNORE INTO domain_evidence VALUES (?,?)", ((key, v) for v in evidence)
        )

    def load_session(self, namespace: str) -> Session | None:
        rows = self._rows(
            "SELECT layout_version,payload FROM sessions WHERE namespace=?", (namespace,)
        )
        if not rows:
            return None
        if rows[0][0] != sessions.LAYOUT_VERSION:
            raise PersistenceError("unsupported durable session layout")
        return sessions.loads(cast(bytes, rows[0][1]))

    def checkpoint(self, provider: str, resource: str) -> Checkpoint | None:
        rows = self._rows(
            "SELECT payload FROM checkpoints WHERE provider=? AND resource=?", (provider, resource)
        )
        if not rows:
            return None
        value = _typed(cast(bytes, rows[0][0]), Checkpoint)
        if (value.provider_id, value.resource_id) != (provider, resource):
            raise PersistenceError("checkpoint index disagrees with stored resource")
        return value

    def advance_checkpoint(self, value: Checkpoint, expected_generation: int) -> None:
        if self._depth == 0 or self._owner != get_ident():
            raise PersistenceError("checkpoint requires linked ingestion transaction")
        previous = self.checkpoint(value.provider_id, value.resource_id)
        generation = 0 if previous is None else previous.generation
        if generation != expected_generation or value.generation != generation + 1:
            raise PersistenceError("checkpoint generation conflict")
        if previous is not None and value.completed_ns < previous.completed_ns:
            raise TemporalConsistencyError("checkpoint completion must advance")
        self._connection.execute(
            "INSERT INTO checkpoints VALUES (?,?,?,?) ON CONFLICT(provider,resource) DO "
            "UPDATE SET generation=excluded.generation,payload=excluded.payload",
            (value.provider_id, value.resource_id, value.generation, codec.dumps(value)),
        )

    def record_operation(self, provider: str, available_ns: int, kind: str, value: object) -> None:
        require_ns(available_ns)
        with self.transaction():
            self._connection.execute(
                "INSERT INTO operations(provider,available,kind,payload) VALUES (?,?,?,?)",
                (provider, available_ns, kind, codec.dumps(value)),
            )

    def operations(self, provider: str, kind: str) -> tuple[object, ...]:
        return tuple(
            codec.loads(cast(bytes, r[0]))
            for r in self._rows(
                "SELECT payload FROM operations WHERE provider=? AND kind=? ORDER BY available,id",
                (provider, kind),
            )
        )

    def quarantine(self, value: Quarantine) -> None:
        with self.transaction():
            self._connection.execute(
                "INSERT INTO quarantine VALUES (?,?,?,?,?) ON "
                "CONFLICT(provider,resource,hash,adapter_version) DO UPDATE SET "
                "payload=excluded.payload",
                (
                    value.provider_id,
                    value.resource_id,
                    value.payload_hash,
                    value.adapter_version,
                    codec.dumps(value),
                ),
            )

    def quarantined(
        self, provider: str, resource: str, payload_hash: str, adapter_version: str
    ) -> Quarantine | None:
        rows = self._rows(
            "SELECT payload FROM quarantine WHERE provider=? AND resource=? AND hash=? AND "
            "adapter_version=?",
            (provider, resource, payload_hash, adapter_version),
        )
        return None if not rows else _typed(cast(bytes, rows[0][0]), Quarantine)

    def clear_quarantine(
        self, provider: str, resource: str, payload_hash: str, adapter_version: str
    ) -> bool:
        with self.transaction():
            return (
                self._connection.execute(
                    "DELETE FROM quarantine WHERE provider=? AND resource=? AND hash=? AND "
                    "adapter_version=?",
                    (provider, resource, payload_hash, adapter_version),
                ).rowcount
                > 0
            )

    def acquisition(
        self,
        provider: str,
        resource: str,
        observed_ns: int,
        payload: bytes,
        metadata: object,
        policy: RetentionPolicy,
    ) -> None:
        from hashlib import sha256

        digest = sha256(payload).hexdigest()
        raw = None
        if policy.mode is RetentionMode.BOUNDED_RAW:
            if len(payload) > policy.max_raw_bytes:
                raise ValidationError("raw retention size exceeded")
            raw = payload
        key = fingerprint("eie:acquisition:v1", (provider, resource, observed_ns, digest))
        with self.transaction():
            self._connection.execute(
                "INSERT OR IGNORE INTO acquisitions VALUES (?,?,?,?,?,?,?)",
                (key, provider, resource, observed_ns, digest, codec.dumps(metadata), raw),
            )

    def validate(self) -> tuple[str, ...]:
        errors = tuple(str(r[0]) for r in self._rows("PRAGMA integrity_check") if r[0] != "ok")
        errors += tuple("FOREIGN_KEY_VIOLATION" for _ in self._rows("PRAGMA foreign_key_check"))
        try:
            for row in self._rows("SELECT id,revision,available,payload FROM events"):
                _checked_event(row)
        except PersistenceError:
            errors += ("INVALID_EVENT_INDEX",)
        for table in (
            "observations",
            "events",
            "domain_records",
            "checkpoints",
            "operations",
            "quarantine",
        ):
            for row in self._rows(f"SELECT payload FROM {table}"):
                try:
                    codec.loads(cast(bytes, row[0]))
                except PersistenceError:
                    errors += (f"INVALID_CONTRACT:{table}",)
                    break
        for row in self._rows("SELECT namespace FROM sessions"):
            try:
                self.load_session(cast(str, row[0]))
            except PersistenceError:
                errors += ("INVALID_SESSION",)
                break
        return errors

    def counts(self) -> dict[str, int]:
        return {
            name: int(cast(int, self._rows(f"SELECT COUNT(*) FROM {name}")[0][0]))
            for name in (
                "observations",
                "events",
                "domain_records",
                "sessions",
                "checkpoints",
                "operations",
                "quarantine",
                "acquisitions",
            )
        }

    @property
    def schema_version(self) -> str:
        return str(
            self._rows(
                "SELECT schema_version FROM schema_migrations ORDER BY version DESC LIMIT 1"
            )[0][0]
        )

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                if self._depth:
                    raise PersistenceError("cannot close active transaction")
                self._connection.close()
                self._closed = True

    def __enter__(self) -> "SQLiteEventStore":
        self._check()
        return self

    def __exit__(self, *args: object) -> None:
        self.close()
