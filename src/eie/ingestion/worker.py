"""Atomic prepare/persist/checkpoint pipeline, without network operations."""

from collections.abc import Callable
from hashlib import sha256
from threading import Event

from eie.core.errors import PersistenceError
from eie.ingestion.checkpoints import resume_cursor
from eie.ingestion.models import Checkpoint, FailureClass, Latencies, PollResult, Quarantine
from eie.ingestion.processors import IngestionFailure
from eie.ingestion.queue import QueueItem
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import RuntimeClock
from eie.runtime.providers import ProviderRegistration
from eie.transport.errors import AcquisitionCancelled


class BatchWriter:
    def __init__(
        self,
        store: SQLiteEventStore,
        clock: RuntimeClock,
        force: Event,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.store, self.clock, self.force, self.fault = store, clock, force, fault

    def boundary(self, name: str) -> None:
        if self.fault is not None:
            self.fault(name)
        if self.force.is_set():
            raise AcquisitionCancelled("ingestion stopped before commit")

    def process(self, item: QueueItem, registration: ProviderRegistration) -> PollResult:
        response, processor = item.acquired.response, registration.processor
        resource = registration.source.resource_id
        digest = sha256(response.body).hexdigest()
        started = self.clock.monotonic_ns()
        prepare_ns = commit_ns = 0
        failure = FailureClass.PERSISTENCE_FAILURE
        old = self.store.checkpoint(item.provider_id, resource)
        generation = 0 if old is None else old.generation
        if response.status not in (200, 304):
            self.store.acquisition(
                item.provider_id,
                resource,
                item.observed_ns,
                response.body,
                {"status": response.status, "headers": response.headers},
                registration.retention,
            )
            category = (
                FailureClass.RATE_LIMIT
                if response.status == 429
                else FailureClass.SOURCE_UNAVAILABLE
            )
            return self.failed(item, category, generation)
        quarantined = self.store.quarantined(
            item.provider_id, resource, digest, processor.adapter_version
        )
        if quarantined is not None:
            return self.failed(item, quarantined.category, generation, quarantined=True)
        committed = False
        try:
            with self.store.transaction():
                self.boundary("before_prepare")
                # Read state under the serialized writer transaction, avoiding lost updates.
                old = self.store.checkpoint(item.provider_id, resource)
                generation = 0 if old is None else old.generation
                ingested = self.clock.utc_ns()
                if ingested < item.observed_ns or (
                    old is not None and ingested <= old.completed_ns
                ):
                    raise IngestionFailure(FailureClass.CONFIGURATION_FAILURE)
                batch = None
                if response.status == 200:
                    session = (
                        self.store.load_session(processor.namespace) or processor.initial_session()
                    )
                    failure = FailureClass.NORMALIZATION_FAILURE
                    batch = processor.prepare(
                        session, response.body, item.observed_ns, ingested, boundary=self.boundary
                    )
                    prepare_ns = self.clock.monotonic_ns() - started
                    self.boundary("after_prepare")
                    self.boundary("after_raw_preparation")
                    failure = FailureClass.PERSISTENCE_FAILURE
                    commit_started = self.clock.monotonic_ns()
                    self.store.save_session(processor.namespace, batch.session)
                else:
                    if old is None:
                        raise IngestionFailure(FailureClass.CONFIGURATION_FAILURE)
                    commit_started = self.clock.monotonic_ns()
                self.store.acquisition(
                    item.provider_id,
                    resource,
                    item.observed_ns,
                    response.body,
                    {"status": response.status, "headers": response.headers},
                    registration.retention,
                )
                self.boundary("after_persist")
                failure = FailureClass.CHECKPOINT_FAILURE
                completed = max(item.observed_ns, self.clock.utc_ns())
                checkpoint = Checkpoint(
                    item.provider_id,
                    resource,
                    generation + 1,
                    item.observed_ns,
                    completed,
                    resume_cursor(
                        None if old is None else old.cursor,
                        item.acquired.cursor
                        or (
                            batch.cursor
                            if batch is not None
                            else old.cursor
                            if old is not None
                            else None
                        ),
                    ),
                    batch.last_source_item
                    if batch is not None
                    else old.last_source_item
                    if old is not None
                    else None,
                    response.header("etag") or (old.etag if old is not None else None),
                    response.header("last-modified")
                    or (old.last_modified if old is not None else None),
                )
                self.boundary("before_checkpoint")
                self.store.advance_checkpoint(checkpoint, generation)
                self.boundary("before_commit")
            committed = True
            commit_ns = self.clock.monotonic_ns() - commit_started
            if self.fault is not None:
                self.fault("after_commit")
            return PollResult(
                item.provider_id,
                resource,
                item.observed_ns,
                max(completed, self.clock.utc_ns()),
                response.status,
                True,
                items_ingested=0 if batch is None else min(batch.items_seen, batch.raw_created),
                duplicates=0 if batch is None else batch.duplicates,
                checkpoint_generation=checkpoint.generation,
                items_fetched=0 if batch is None else batch.items_seen,
                raw_created=0 if batch is None else batch.raw_created,
                domain_created=0 if batch is None else batch.domain_created,
                latencies=Latencies(
                    item.fetched_mono_ns - item.fetch_started_mono_ns,
                    started - item.enqueued_mono_ns,
                    prepare_ns,
                    commit_ns,
                    self.clock.monotonic_ns() - item.fetch_started_mono_ns,
                    parse_ns=0 if batch is None else batch.parse_ns,
                    normalize_ns=0 if batch is None else batch.normalize_ns,
                ),
            )
        except Exception as exc:
            if committed:
                raise
            if isinstance(exc, IngestionFailure):
                failure = exc.category
            elif isinstance(exc, PersistenceError):
                failure = (
                    failure
                    if failure is FailureClass.CHECKPOINT_FAILURE
                    else FailureClass.PERSISTENCE_FAILURE
                )
            elif isinstance(exc, AcquisitionCancelled):
                return self.failed(item, FailureClass.SOURCE_UNAVAILABLE, generation)
            else:
                failure = FailureClass.NORMALIZATION_FAILURE
        if failure in (
            FailureClass.PARSE_FAILURE,
            FailureClass.SCHEMA_MISMATCH,
            FailureClass.NORMALIZATION_FAILURE,
        ):
            now = max(item.observed_ns, self.clock.utc_ns())
            with self.store.transaction():
                self.store.acquisition(
                    item.provider_id,
                    resource,
                    item.observed_ns,
                    response.body,
                    {"status": response.status},
                    registration.retention,
                )
                self.store.quarantine(
                    Quarantine(
                        item.provider_id,
                        resource,
                        digest,
                        processor.adapter_version,
                        failure,
                        now,
                        now,
                    )
                )
            return self.failed(item, failure, generation, quarantined=True)
        return self.failed(item, failure, generation)

    def failed(
        self,
        item: QueueItem,
        category: FailureClass,
        generation: int = 0,
        *,
        quarantined: bool = False,
    ) -> PollResult:
        return PollResult(
            item.provider_id,
            self._resource(item),
            item.observed_ns,
            max(item.observed_ns, self.clock.utc_ns()),
            item.acquired.response.status,
            False,
            checkpoint_generation=generation,
            error_category=category,
            quarantined=quarantined,
            latencies=Latencies(
                fetch_ns=item.fetched_mono_ns - item.fetch_started_mono_ns,
                total_ns=self.clock.monotonic_ns() - item.fetch_started_mono_ns,
            ),
        )

    def _resource(self, item: QueueItem) -> str:
        # The resource comes from trusted registration, never from provider bytes.
        return self.resources[item.provider_id]

    resources: dict[str, str]
