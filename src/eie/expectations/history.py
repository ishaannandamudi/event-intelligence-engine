"""Append-only observation ledger; insertion validates raw evidence and revision chains."""

from eie.core.errors import PersistenceError, TemporalConsistencyError
from eie.core.serialization import canonical_json_bytes
from eie.events.revisions import RevisionKind
from eie.expectations.queries import Observation
from eie.persistence.memory import InMemoryEventStore


class ObservationLedger:
    def __init__(self) -> None:
        self._streams: dict[str, list[Observation]] = {}

    def append(self, row: Observation, store: InMemoryEventStore) -> bool:
        history = self._streams.get(row.stream_id, [])
        if row.revision.number <= len(history):
            if row != history[row.revision.number - 1]:
                raise PersistenceError("expectation revision identity collision")
            return False
        if row.revision.number != len(history) + 1:
            raise PersistenceError("expectation revision gap")
        if history and row.source.available_from_ns <= history[-1].source.available_from_ns:
            raise TemporalConsistencyError("expectation knowledge must advance")
        if not history and row.revision.kind is not RevisionKind.ORIGINAL:
            raise PersistenceError("first expectation revision must be original")
        raw = store.get_observation(row.source.source_observation_id)
        if raw is None:
            raise PersistenceError("expectation lacks raw evidence")
        if raw.ingested_at_ns > row.source.available_from_ns:
            raise TemporalConsistencyError("expectation predates raw evidence")
        if (raw.source_observed_at_ns, raw.ingested_at_ns) != (
            row.source.provider_observed_at_ns,
            row.source.ingested_at_ns,
        ):
            raise TemporalConsistencyError("expectation clocks disagree with raw evidence")
        if (raw.source.name, raw.source.source_class, raw.source_item_id) != (
            row.source.provider_id,
            row.source.source_class,
            row.source.provider_source_item_id,
        ):
            raise PersistenceError("expectation provenance disagrees with raw evidence")
        self._streams.setdefault(row.stream_id, []).append(row)
        return True

    def all(self) -> tuple[Observation, ...]:
        return tuple(
            sorted(
                (v for key in sorted(self._streams) for v in self._streams[key]),
                key=lambda v: (v.source.available_from_ns, v.stream_id, v.revision.number),
            )
        )

    def history(self, stream_id: str) -> tuple[Observation, ...]:
        return tuple(self._streams.get(stream_id, ()))

    def canonical_state_bytes(self) -> bytes:
        return canonical_json_bytes(self.all())
