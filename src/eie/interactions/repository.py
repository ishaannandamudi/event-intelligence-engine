"""Immutable compact event index and interaction artifacts on the shared SQLite store."""

from typing import TypeVar

from eie.core.errors import PersistenceError, ValidationError
from eie.evidence.repository import EvidenceRepository
from eie.interactions.inputs import build_event
from eie.interactions.models import (
    DAY,
    Artifact,
    EventCollision,
    EventSequence,
    EventStateTransition,
    InteractionContext,
    InteractionEvent,
    InteractionInputManifest,
    InteractionPolicy,
    InteractionRunManifest,
    InteractionWindow,
    PathReference,
    PropagationChain,
    PropagationChronology,
)
from eie.interactions.schema import OrderingBasis
from eie.interactions.windows import interval
from eie.persistence.sqlite import SQLiteEventStore

T = TypeVar("T", bound=Artifact)


class InteractionRepository:
    def __init__(self, store: SQLiteEventStore) -> None:
        self.store, self.evidence = store, EvidenceRepository(store)

    @staticmethod
    def indexes(row: InteractionEvent) -> tuple[object, ...]:
        occurrence = interval(row.occurrence, coarse=True)
        schedule = interval(row.scheduled, coarse=True)
        return (
            row.artifact_id,
            row.dataset_id,
            row.event_id,
            row.revision_number,
            row.revision_id,
            row.event_type.family.value,
            row.event_type.code,
            row.scope.kind.value,
            row.known_ns,
            row.source_available_ns,
            None if occurrence is None else occurrence[0],
            None if occurrence is None else occurrence[1],
            None if schedule is None else schedule[0],
            None if schedule is None else schedule[1],
            row.thread_id,
            row.fingerprint,
        )

    def index_event(self, dataset: str, event_id: str, known: int) -> InteractionEvent:
        with self.store.transaction():
            event = self.store.get_event(event_id, known)
            if event is None:
                raise ValidationError("interaction index requires a visible canonical event")
            value = build_event(self.evidence, dataset, event)
            if not self.store._rows(
                "SELECT id FROM interaction_events WHERE id=?", (value.artifact_id,)
            ) and self.store._rows(
                "SELECT 1 FROM interaction_seals WHERE dataset=? AND known>=? LIMIT 1",
                (dataset, value.known_ns),
            ):
                raise ValidationError("cannot backdate interaction index into a sealed cutoff")
            if self.evidence._put("interaction_events", self.indexes(value), value):
                self.store._connection.executemany(
                    "INSERT INTO interaction_entities VALUES (?,?)",
                    ((value.artifact_id, i) for i in value.scope.identifiers),
                )
                self.store._connection.executemany(
                    "INSERT INTO interaction_bundles VALUES (?,?)",
                    ((value.artifact_id, i) for i in value.bundle_ids),
                )
            return value

    def event(self, identity: str, *, deep: bool = False) -> InteractionEvent:
        item = self.evidence._one("interaction_events", identity, InteractionEvent)
        if item is None or item[1] != self.indexes(item[0]):
            raise PersistenceError("missing/corrupt interaction event index")
        row = item[0]
        for table, field, desired in (
            ("interaction_entities", "entity", row.scope.identifiers),
            ("interaction_bundles", "bundle", row.bundle_ids),
        ):
            if self.store._rows(
                f"SELECT {field} FROM {table} WHERE row=? ORDER BY {field}", (identity,)
            ) != [(i,) for i in desired]:
                raise PersistenceError("interaction index membership disagreement")
        if deep:
            source = self.store.get_event(row.event_id, row.known_ns)
            if source is None or row != build_event(self.evidence, row.dataset_id, source):
                raise PersistenceError("interaction event source recalculation disagreement")
        return row

    def latest(self, dataset: str, event: str, known: int) -> InteractionEvent:
        ids = self.evidence._ids(
            "SELECT id FROM interaction_events WHERE dataset=? AND event=? AND known<=? "
            "ORDER BY revision DESC",
            (dataset, event, known),
            10000,
        )
        if not ids:
            raise ValidationError("target event is absent from interaction index at cutoff")
        return self.event(ids[0])

    @staticmethod
    def artifact_indexes(value: Artifact) -> tuple[object, ...]:
        subject: str | None = None
        known = 0
        if isinstance(value, (InteractionPolicy, InteractionWindow)):
            pass
        elif isinstance(value, EventCollision):
            subject, known = value.left_row_id, value.known_as_of_ns
        elif isinstance(value, EventSequence):
            subject, known = value.entity_id, value.known_as_of_ns
        elif isinstance(value, EventStateTransition):
            subject, known = value.thread_id, value.known_ns
        elif isinstance(value, PathReference):
            subject, known = value.node_ids[0], value.known_ns
        elif isinstance(value, PropagationChronology):
            subject, known = value.root_row_id, value.later_known_ns
        elif isinstance(value, PropagationChain):
            subject, known = value.root_row_id, value.known_as_of_ns
        elif isinstance(value, InteractionInputManifest):
            subject, known = value.query.target_event_id, value.query.known_as_of_ns
        elif isinstance(value, InteractionRunManifest):
            known = value.known_as_of_ns
        else:
            raise ValidationError("unsupported interaction storage artifact")
        return value.artifact_id, type(value).__name__, subject, known, value.fingerprint

    def put(self, value: Artifact) -> None:
        self.evidence.owned()
        self.evidence._put("interaction_artifacts", self.artifact_indexes(value), value)

    def artifact(self, identity: str, cls: type[T]) -> T:
        item = self.evidence._one("interaction_artifacts", identity, cls)
        if item is None or item[1] != self.artifact_indexes(item[0]):
            raise PersistenceError("missing/corrupt interaction artifact")
        return item[0]

    def append_policy(self, policy: InteractionPolicy) -> str:
        with self.store.transaction():
            self.put(policy)
            self.put(policy.window)
        return policy.artifact_id

    def policy(self, identity: str) -> InteractionPolicy:
        return self.artifact(identity, InteractionPolicy)

    def search(
        self,
        dataset: str,
        known: int,
        basis: OrderingBasis,
        start: int,
        end: int,
        maximum: int,
        *,
        entities: tuple[str, ...] = (),
        bundles: tuple[str, ...] = (),
        broad: bool = False,
        thread: str | None = None,
        history: bool = False,
    ) -> tuple[InteractionEvent, ...]:
        columns = {
            OrderingBasis.EXACT_EVENT_TIME: ("lo", "hi"),
            OrderingBasis.SCHEDULED_TIME: ("schedule_lo", "schedule_hi"),
            OrderingBasis.SOURCE_AVAILABILITY: ("source_time", "source_time"),
            OrderingBasis.KNOWLEDGE_TIME: ("known", "known"),
            OrderingBasis.COARSE_SESSION: ("lo", "hi"),
            OrderingBasis.UNKNOWN: ("known", "known"),
        }
        lo, hi = columns[basis]
        clauses, args = ["e.dataset=?", "e.known<=?"], [dataset, known]
        if not history:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM interaction_events newer WHERE "
                "newer.dataset=e.dataset AND newer.event=e.event AND "
                "newer.revision>e.revision AND newer.known<=?)"
            )
            args.append(known)
        time_clause = f"(e.{lo}>=? AND e.{lo}<=? AND e.{hi}>=?)"
        slack = DAY if lo in ("lo", "schedule_lo") else 0
        time_args: list[object] = [max(0, start - slack), end, start]
        relations = []
        relation_args: list[object] = []
        if entities:
            relations.append(
                "EXISTS (SELECT 1 FROM interaction_entities m WHERE m.row=e.id AND m.entity IN ("
                + ",".join("?" for _ in entities)
                + "))"
            )
            relation_args.extend(entities)
        if broad:
            relations.append(
                "(e.family IN ('MACRO','CENTRAL_BANK') OR e.scope IN ('MARKET','MACRO_GLO"
                "BAL','MACRO_COUNTRY'))"
            )
        if relations:
            time_clause += " AND (" + " OR ".join(relations) + ")"
            time_args.extend(relation_args)
        if bundles:
            time_clause = (
                "("
                + time_clause
                + ") OR EXISTS (SELECT 1 FROM interaction_bundles b WHERE b.row=e.id "
                "AND b.bundle IN (" + ",".join("?" for _ in bundles) + "))"
            )
            time_args.extend(bundles)
        clauses.append("(" + time_clause + ")")
        args.extend(time_args)
        if thread is not None:
            clauses.append("e.thread=?")
            args.append(thread)
        ids = self.evidence._ids(
            "SELECT e.id FROM interaction_events e WHERE "
            + " AND ".join(clauses)
            + " ORDER BY e.id",
            tuple(args),
            maximum,
        )
        return tuple(self.event(i) for i in ids)

    def save_context(
        self, context: InteractionContext, request_id: str, manifest: InteractionInputManifest
    ) -> None:
        self.evidence.owned()
        self.put(manifest)
        q = context.query
        if self.evidence._put(
            "interaction_contexts",
            (
                context.artifact_id,
                request_id,
                q.dataset_id,
                q.target_event_id,
                q.known_as_of_ns,
                q.policy_id,
                manifest.artifact_id,
                context.fingerprint,
            ),
            context,
        ):
            for kind, values in self.members(context).items():
                self.store._connection.executemany(
                    "INSERT INTO interaction_members VALUES (?,?,?)",
                    ((context.artifact_id, kind, v) for v in values),
                )
            self.store._connection.execute(
                "INSERT OR IGNORE INTO interaction_seals VALUES (?,?)",
                (q.dataset_id, q.known_as_of_ns),
            )

    @staticmethod
    def members(context: InteractionContext) -> dict[str, tuple[str, ...]]:
        return {
            "event": context.member_row_ids,
            "collision": context.collision_ids,
            "sequence": context.sequence_ids,
            "transition": context.transition_ids,
            "chronology": context.chronology_ids,
            "chain": context.chain_ids,
            "path": context.path_ids,
        }

    def context(self, identity: str) -> InteractionContext:
        item = self.evidence._one("interaction_contexts", identity, InteractionContext)
        if item is None:
            raise PersistenceError("missing interaction context")
        value, scalars = item
        q = value.query
        if (
            scalars[0] != value.artifact_id
            or scalars[2:6] != (q.dataset_id, q.target_event_id, q.known_as_of_ns, q.policy_id)
            or scalars[-1] != value.fingerprint
        ):
            raise PersistenceError("interaction context index disagreement")
        desired = sorted((kind, i) for kind, ids in self.members(value).items() for i in ids)
        if (
            self.store._rows(
                "SELECT kind,member FROM interaction_members WHERE owner=? ORDER BY kind,member",
                (identity,),
            )
            != desired
        ):
            raise PersistenceError("interaction context membership disagreement")
        from eie.interactions.validation import validate

        validate(self, identity)
        return value

    def manifest(self, context_id: str) -> InteractionInputManifest:
        rows = self.store._rows(
            "SELECT manifest FROM interaction_contexts WHERE id=?", (context_id,)
        )
        if not rows:
            raise PersistenceError("missing interaction manifest")
        return self.artifact(str(rows[0][0]), InteractionInputManifest)

    def append_run(self, contexts: tuple[InteractionContext, ...]) -> InteractionRunManifest:
        if not contexts:
            raise ValidationError("interaction run requires members")
        first = contexts[0].query
        policy = self.policy(first.policy_id)
        if len(contexts) > policy.max_run_members or any(
            c.query.policy_id != first.policy_id or c.query.known_as_of_ns != first.known_as_of_ns
            for c in contexts
        ):
            raise ValidationError("bounded run must share policy and cutoff")
        ordered = sorted(contexts, key=lambda c: c.artifact_id)
        if len({c.artifact_id for c in ordered}) != len(ordered):
            raise ValidationError("run requires distinct contexts")
        run = InteractionRunManifest(
            first.policy_id,
            first.known_as_of_ns,
            tuple(c.artifact_id for c in ordered),
            tuple(c.fingerprint for c in ordered),
        )
        with self.store.transaction():
            for c in ordered:
                if self.context(c.artifact_id) != c:
                    raise ValidationError("run context must be retained exactly")
            self.put(run)
        return run


ARTIFACT_TYPES = (
    EventCollision,
    EventSequence,
    EventStateTransition,
    PathReference,
    PropagationChronology,
    PropagationChain,
)
