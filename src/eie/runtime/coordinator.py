"""Small bounded thread runtime: scheduling, acquisition, queue and health."""

import time
from collections.abc import Callable, Iterable
from dataclasses import replace
from queue import Empty, Queue
from threading import Event, RLock, Thread
from types import MappingProxyType

from eie.core.errors import PersistenceError, ValidationError
from eie.ingestion.models import FailureClass, PollResult, RuntimeHealth, RuntimeHealthState
from eie.ingestion.queue import IngestionQueue, QueueItem
from eie.ingestion.worker import BatchWriter
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import RuntimeClock, SystemClock
from eie.runtime.config import RuntimeConfig
from eie.runtime.diagnostics import RuntimeMetrics, log_result
from eie.runtime.providers import ProviderRegistration
from eie.transport.errors import AcquisitionCancelled, TransportError, TransportPolicyError


class IngestionCoordinator:
    def __init__(
        self,
        store: SQLiteEventStore,
        registrations: Iterable[ProviderRegistration],
        config: RuntimeConfig | None = None,
        *,
        clock: RuntimeClock | None = None,
        fault: Callable[[str], None] | None = None,
    ) -> None:
        self.store, self.config, self.clock = (
            store,
            config or RuntimeConfig(),
            clock or SystemClock(),
        )
        providers: dict[str, ProviderRegistration] = {}
        namespaces: dict[str, object] = {}
        for registration in registrations:
            provider = registration.source.provider_id
            if provider in providers:
                raise ValidationError("duplicate runtime provider ID")
            processor = registration.processor
            seed = processor.initial_session()
            settings = getattr(
                seed, "settings", getattr(getattr(seed, "engine", None), "settings", None)
            )
            saved = store.load_session(processor.namespace)
            saved_settings = (
                settings
                if saved is None
                else getattr(
                    saved, "settings", getattr(getattr(saved, "engine", None), "settings", None)
                )
            )
            if saved_settings != settings or (
                processor.namespace in namespaces and namespaces[processor.namespace] != settings
            ):
                raise ValidationError(
                    "domain configuration changed; explicit new namespace required"
                )
            namespaces[processor.namespace] = settings
            providers[provider] = registration
        self.providers = MappingProxyType(dict(sorted(providers.items())))
        self.queue = IngestionQueue(self.config.queue_capacity)
        self.metrics = RuntimeMetrics()
        self._lock, self._stop, self._force, self._workers_stop = RLock(), Event(), Event(), Event()
        self._busy: set[str] = set()
        self._due = dict.fromkeys(self.providers, 0)
        self._health: dict[str, RuntimeHealth] = {}
        self._fetch_queue: Queue[str] = Queue(self.config.fetch_count)
        self._fetchers: list[Thread] = []
        self._workers: list[Thread] = []
        self._scheduler: Thread | None = None
        self._started = self._closed = False
        self.fatal_error: FailureClass | None = None
        self._writer = BatchWriter(store, self.clock, self._force, fault)
        self._writer.resources = {p: r.source.resource_id for p, r in self.providers.items()}
        for provider, registration in self.providers.items():
            history = store.operations(provider, "health")
            if history and isinstance(history[-1], RuntimeHealth):
                self._health[provider] = history[-1]
            else:
                checkpoint = store.checkpoint(provider, registration.source.resource_id)
                self._health[provider] = (
                    RuntimeHealth(provider, self.clock.utc_ns(), RuntimeHealthState.UNKNOWN)
                    if checkpoint is None
                    else RuntimeHealth(
                        provider,
                        checkpoint.completed_ns,
                        RuntimeHealthState.HEALTHY,
                        checkpoint.completed_ns,
                        checkpoint.completed_ns,
                    )
                )
            for result in store.operations(provider, "poll"):
                if isinstance(result, PollResult):
                    self.metrics.record(result)

    def _registration(self, provider: str) -> ProviderRegistration:
        if self._closed or self._stop.is_set() or not self.config.enabled:
            raise ValidationError("runtime is disabled or stopped")
        registration = self.providers.get(provider)
        if registration is None or not registration.enabled:
            raise ValidationError("provider is unknown or disabled")
        return registration

    def fetch_once(self, provider: str, *, reserved: bool = False) -> PollResult | None:
        registration = self._registration(provider)
        with self._lock:
            if provider in self._busy and not reserved:
                raise ValidationError("provider poll already in flight")
            if not reserved and len(self._busy) >= self.config.fetch_count:
                raise ValidationError("runtime acquisition capacity exhausted")
            self._busy.add(provider)
        start = self.clock.monotonic_ns()
        try:
            acquired = registration.source.poll(
                self.store.checkpoint(provider, registration.source.resource_id), self._stop
            )
            observed, fetched = self.clock.utc_ns(), self.clock.monotonic_ns()
            self._writer.boundary("after_fetch")
            if len(acquired.response.body) > registration.max_payload_bytes:
                raise TransportPolicyError("provider payload exceeds runtime bound")
            item = QueueItem(
                provider, acquired, observed, start, fetched, self.clock.monotonic_ns()
            )
            if not self.queue.offer(item):
                return self._finish(self._writer.failed(item, FailureClass.BACKPRESSURE))
            return None
        except Exception as exc:
            category = (
                FailureClass.PERSISTENCE_FAILURE
                if isinstance(exc, PersistenceError)
                else FailureClass.CONFIGURATION_FAILURE
                if isinstance(exc, TransportPolicyError)
                else FailureClass.SOURCE_UNAVAILABLE
                if isinstance(exc, AcquisitionCancelled)
                else FailureClass.TRANSPORT_FAILURE
                if isinstance(exc, (TransportError, ValidationError))
                else FailureClass.SOURCE_UNAVAILABLE
            )
            if category is FailureClass.PERSISTENCE_FAILURE:
                self.fatal_error = category
                self._stop.set()
                self._force.set()
            now = self.clock.utc_ns()
            return self._finish(
                PollResult(
                    provider,
                    registration.source.resource_id,
                    now,
                    now,
                    None,
                    False,
                    error_category=category,
                )
            )
        except BaseException:
            with self._lock:
                self._busy.discard(provider)
            raise

    def ingest_once(self, provider: str) -> PollResult:
        if self._started:
            raise ValidationError("ingest-once requires an idle coordinator")
        result = self.fetch_once(provider)
        if result is not None:
            return result
        while (result := self.process_next()) is not None:
            if result.provider_id == provider:
                return result
        raise PersistenceError("queued poll missing")

    def process_next(self, timeout: float = 0) -> PollResult | None:
        item = self.queue.take(timeout)
        if item is None:
            return None
        try:
            result = self._writer.process(item, self.providers[item.provider_id])
            if result.error_category is FailureClass.PERSISTENCE_FAILURE:
                self.fatal_error = FailureClass.PERSISTENCE_FAILURE
                self._stop.set()
                self._force.set()
            return self._finish(result)
        except PersistenceError:
            self.fatal_error = FailureClass.PERSISTENCE_FAILURE
            self._stop.set()
            self._force.set()
            return self._finish(self._writer.failed(item, FailureClass.PERSISTENCE_FAILURE))
        finally:
            self.queue.complete()
            with self._lock:
                self._busy.discard(item.provider_id)

    def _finish(self, result: PollResult) -> PollResult:
        with self._lock:
            previous = self._health[result.provider_id]
            success = result.committed
            states: dict[FailureClass | None, RuntimeHealthState] = {
                FailureClass.RATE_LIMIT: RuntimeHealthState.RATE_LIMITED,
                FailureClass.SCHEMA_MISMATCH: RuntimeHealthState.SCHEMA_MISMATCH,
                FailureClass.PARSE_FAILURE: RuntimeHealthState.SCHEMA_MISMATCH,
                FailureClass.SOURCE_UNAVAILABLE: RuntimeHealthState.UNAVAILABLE,
            }
            state = (
                RuntimeHealthState.HEALTHY
                if success
                else states.get(result.error_category, RuntimeHealthState.DEGRADED)
            )
            health = RuntimeHealth(
                result.provider_id,
                max(result.completed_ns, previous.available_ns),
                state,
                result.observed_ns,
                result.completed_ns if success else previous.last_success_ns,
                0 if success else previous.consecutive_failures + 1,
                result.error_category,
            )
            self._health[result.provider_id] = health
            self.metrics.record(result)
            self._busy.discard(result.provider_id)
            try:
                with self.store.transaction():
                    self.store.record_operation(
                        result.provider_id, result.completed_ns, "poll", result
                    )
                    self.store.record_operation(
                        result.provider_id, health.available_ns, "health", health
                    )
            except PersistenceError:
                self.fatal_error = FailureClass.PERSISTENCE_FAILURE
                self._stop.set()
                self._force.set()
            log_result(result)
            return result

    def health(self, provider: str) -> RuntimeHealth:
        with self._lock:
            value = self._health[provider]
            now = max(value.available_ns, self.clock.utc_ns())
            if (
                value.state is RuntimeHealthState.HEALTHY
                and value.last_success_ns is not None
                and now - value.last_success_ns
                > int(self.providers[provider].stale_after * 1_000_000_000)
            ):
                return replace(value, available_ns=now, state=RuntimeHealthState.STALE)
            return value

    def start(self, *, schedule: bool = True) -> None:
        if self._started or self._closed or not self.config.enabled or self._stop.is_set():
            raise ValidationError("runtime cannot start")
        self._started = True
        self._workers = [
            Thread(target=self._work_loop, name=f"eie-write-{i}", daemon=True)
            for i in range(self.config.worker_count)
        ]
        self._fetchers = [
            Thread(target=self._fetch_loop, name=f"eie-fetch-{i}", daemon=True)
            for i in range(self.config.fetch_count)
        ]
        for thread in (*self._workers, *self._fetchers):
            thread.start()
        if schedule:
            self._scheduler = Thread(target=self._schedule_loop, name="eie-poll", daemon=True)
            self._scheduler.start()

    def tick(self) -> tuple[str, ...]:
        if not self._started or self._stop.is_set():
            return ()
        dispatched: list[str] = []
        with self._lock:
            now = self.clock.monotonic_ns()
            for provider, registration in self.providers.items():
                if len(self._busy) >= self.config.fetch_count:
                    break
                if (
                    registration.enabled
                    and provider not in self._busy
                    and self._due[provider] <= now
                ):
                    self._busy.add(provider)
                    self._due[provider] = now + int(registration.poll_interval * 1_000_000_000)
                    self._fetch_queue.put_nowait(provider)
                    dispatched.append(provider)
        return tuple(dispatched)

    def _schedule_loop(self) -> None:
        while not self._stop.is_set():
            self.tick()
            self._stop.wait(self.config.scheduler_resolution)

    def _fetch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                provider = self._fetch_queue.get(timeout=0.05)
            except Empty:
                continue
            try:
                if not self._stop.is_set():
                    self.fetch_once(provider, reserved=True)
            finally:
                self._fetch_queue.task_done()

    def _work_loop(self) -> None:
        while not self._workers_stop.is_set() or self.queue.depth:
            self.process_next(0.05)

    def request_stop(self) -> None:
        """Signal-safe request; the foreground owner performs bounded shutdown."""
        self._stop.set()

    def wait_for_stop(self, timeout: float = 0.5) -> bool:
        return self._stop.wait(timeout)

    def shutdown(self, *, close_store: bool = True) -> bool:
        if self._closed:
            return True
        self._stop.set()
        deadline = time.monotonic() + self.config.shutdown_timeout
        for thread in ([self._scheduler] if self._scheduler is not None else []) + self._fetchers:
            thread.join(max(0, deadline - time.monotonic()))
        self._workers_stop.set()
        if not self._started:
            if self.queue.depth:
                drain = Thread(target=self._work_loop, name="eie-drain", daemon=True)
                self._workers.append(drain)
                drain.start()
        for thread in self._workers:
            thread.join(max(0, deadline - time.monotonic()))
        graceful = not any(t.is_alive() for t in self._fetchers + self._workers)
        if not graceful:
            self._force.set()
            # Fence pending commits; transaction lock ensures active writes finish/roll back.
            # A blocked custom processor can still hold this lock: do not wait on it here.
            return False
        for provider in self.providers:
            value = self._health[provider]
            stopped = replace(
                value,
                available_ns=max(value.available_ns, self.clock.utc_ns()),
                state=RuntimeHealthState.STOPPED,
            )
            try:
                self.store.record_operation(provider, stopped.available_ns, "health", stopped)
            except PersistenceError:
                self.fatal_error = FailureClass.PERSISTENCE_FAILURE
                graceful = False
            self._health[provider] = stopped
        if close_store:
            self.store.close()
        self._closed = True
        return graceful
