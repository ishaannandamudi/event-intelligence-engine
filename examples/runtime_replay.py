"""Offline fixture poll, atomic commit, restart, replay and duplicate suppression."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from eie.config.settings import Settings
from eie.ingestion.processors import AdapterProcessor
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.sec.adapter import SecAdapter, SecIngestor
from eie.providers.sec.settings import SecSettings
from eie.replay.protocols import StoreReplayReader
from eie.runtime.clock import ManualClock, RuntimeClock
from eie.runtime.config import RuntimeConfig
from eie.runtime.coordinator import IngestionCoordinator
from eie.runtime.providers import ProviderRegistration
from eie.runtime.sources import AcquiredPayload, FixtureSource
from eie.transport.models import HttpResponse

OBSERVED = 1_768_150_800_000_000_000


def provider(clock: RuntimeClock) -> ProviderRegistration:
    payload = (
        Path(__file__).resolve().parents[1] / "tests/fixtures/sec/submissions.json"
    ).read_bytes()
    source = FixtureSource(
        "SEC_EDGAR", "recent", (AcquiredPayload(HttpResponse(200, body=payload)),)
    )
    processor = AdapterProcessor(
        "sec",
        SecIngestor(Settings(sec=SecSettings(enabled=True))),
        SecAdapter(),
        source_url="https://data.sec.gov/submissions/CIK0000123456.json",
        clock=clock,
    )
    return ProviderRegistration(source, processor, enabled=True)


def demo(path: Path) -> dict[str, int]:
    clock = ManualClock(OBSERVED)
    store = SQLiteEventStore(path)
    runtime = IngestionCoordinator(
        store, (provider(clock),), RuntimeConfig(enabled=True), clock=clock
    )
    first = runtime.ingest_once("SEC_EDGAR")
    assert first.committed and first.checkpoint_generation == 1
    historical_bytes = store.canonical_state_bytes()
    assert runtime.shutdown()  # closes the durable store
    clock.advance(1)
    with SQLiteEventStore(path) as reopened:
        reader = StoreReplayReader(reopened)
        assert reader.events_known_at(OBSERVED - 1).events == ()
        assert len(reader.events_known_at(OBSERVED).events) == 1
        runtime = IngestionCoordinator(
            reopened, (provider(clock),), RuntimeConfig(enabled=True), clock=clock
        )
        repeated = runtime.ingest_once("SEC_EDGAR")
        assert repeated.committed and repeated.duplicates == 1
        assert repeated.checkpoint_generation == 2
        assert reopened.canonical_state_bytes() == historical_bytes
        assert reopened.validate() == ()
        return {
            "events": reopened.counts()["events"],
            "generation": repeated.checkpoint_generation,
            "duplicates": repeated.duplicates,
        }


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        print(json.dumps(demo(Path(directory) / "events.sqlite"), sort_keys=True))
