"""Native official SEC current/history traversal uses deterministic fake transport."""

import json
from threading import Event

import pytest
from test_sec_ingestion import CIK_A, HISTORY_URL, T2, T3, T4, URL, amend_payload, fixture, service

from eie.archive.storage import FileArchive
from eie.backfill.coordinator import BackfillCoordinator
from eie.backfill.models import GapReason, JobStatus, PartitionPolicy, TimeRange
from eie.backfill.planner import plan
from eie.backfill.rebuild import rebuild
from eie.backfill.sec import SecHistoricalSource, SecHistoryProcessor, resource_cik, sec_history
from eie.core.errors import ValidationError
from eie.datasets.models import BackfillJob, DatasetDefinition, ValidationStatus
from eie.datasets.queries import DatasetReader
from eie.datasets.repository import DatasetRepository
from eie.datasets.validation import validate_dataset
from eie.filings.models import AmendmentStatus
from eie.ingestion.models import RetentionMode, RetentionPermission, RetentionPolicy
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.sec.models import CIK
from eie.runtime.clock import ManualClock
from eie.transport.errors import TransientTransportError, TransportPolicyError
from eie.transport.models import HttpResponse


class FakeHttp:
    def __init__(self, pages):
        self.pages = pages
        self.requests = []

    def fetch(self, url, policy, conditionals=()):
        policy.validate_url(url)
        self.requests.append(url)
        value = self.pages[url]
        return value if isinstance(value, HttpResponse) else HttpResponse(200, body=value)


def definition(spec):
    return DatasetDefinition(
        "sec-offline-history",
        TimeRange(1704067200000000000, T4 + 1),
        (spec,),
        partition_policy=PartitionPolicy(T4 + 1),
        archive_policy=RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.ALLOWED),
    )


def test_native_multi_cik_history_refs_shared_rate_contact_and_archive_replay(tmp_path):
    clock = ManualClock(T4 + 1)
    second = CIK.parse(7654321)
    other = json.loads(fixture("submissions.json"))
    other["cik"] = second.digits
    other["filings"]["recent"]["accessionNumber"] = [second.digits + "-26-000001"]
    other["filings"]["files"] = []
    other_url = f"https://data.sec.gov/submissions/CIK{second.digits}.json"
    http = FakeHttp(
        {
            URL: fixture("submissions.json"),
            HISTORY_URL: fixture("history.json"),
            other_url: json.dumps(other).encode(),
        }
    )
    source, processor = sec_history(
        (CIK_A, second),
        "EIE research@example.invalid",
        clock,
        transport=http,
        issuer_ciks=(CIK_A, second),
    )
    job = BackfillJob(definition(source.spec))
    # Capability limits split a long range into deterministic year-bounded partitions.
    assert {p.resource_id for p in plan(job)} == {"cik:" + CIK_A.digits, "cik:" + second.digits}
    with (
        SQLiteEventStore(tmp_path / "sec.sqlite") as store,
        FileArchive(tmp_path / "archive") as archive,
    ):
        repo = DatasetRepository(store)
        assert (
            BackfillCoordinator(repo, archive, ((source, processor),), clock=clock).run(job)
            is JobStatus.COMPLETED_WITH_GAPS
        )
        reader = DatasetReader(repo, job.build_id)
        assert len(reader.events_known_at(T2).events) == 3
        assert len(reader.filings(CIK_A, T2)) == 2
        assert all("/submissions/" in url for url in http.requests)
        assert all(f.raw.source_observed_at_ns >= T4 + 1 for f in repo.facts(job.build_id))
        assert {g.reason for g in repo.gaps(job.build_id)} == {GapReason.UNKNOWN_COMPLETENESS}
        assert (
            validate_dataset(repo, archive, job.build_id).status
            is ValidationStatus.VALID_WITH_KNOWN_GAPS
        )
        original = repo.manifest(job.build_id)
        count = len(http.requests)
        rebuilt = rebuild(
            repo, archive, job.build_id, {"SEC_EDGAR": processor}, "archive-only", clock=clock
        )
        assert repo.manifest(rebuilt.build_id).content_fingerprint == original.content_fingerprint
        assert len(http.requests) == count
        assert reader.registry(T2).resolve("OLD", T2).candidate_ids == ()


def test_live_and_historical_duplicate_share_raw_filing_event_identity_without_mutating_live(
    tmp_path,
):
    live = service()
    live.ingest_payload(fixture("submissions.json"), URL, T3, T3)
    metadata = live.filings_for_cik(CIK_A, T3).filings[0].metadata
    live_event = live.event_store.known_as_of(T3)[0]
    raw = live.event_store.get_observation(metadata.source_observation_id)
    clock = ManualClock(T4 + 1)
    http = FakeHttp({URL: fixture("submissions.json"), HISTORY_URL: fixture("history.json")})
    source, processor = sec_history(
        (CIK_A,), "EIE research@example.invalid", clock, transport=http, issuer_ciks=(CIK_A,)
    )
    job = BackfillJob(definition(source.spec))
    with (
        SQLiteEventStore(tmp_path / "sec.sqlite") as store,
        FileArchive(tmp_path / "archive") as archive,
    ):
        store.append_observation(raw)
        store.append_event(live_event)
        saved = store.canonical_state_bytes()
        repo = DatasetRepository(store)
        BackfillCoordinator(repo, archive, ((source, processor),), clock=clock).run(job)
        matching = [f for f in repo.facts(job.build_id) if f.logical_id == live_event.event_id]
        assert len(matching) == 1 and matching[0].raw == raw
        assert store.canonical_state_bytes() == saved
        assert DatasetReader(repo, job.build_id).events_known_at(T2).events[-1].event_id in {
            f.logical_id for f in repo.facts(job.build_id)
        }
        assert any(
            f.record.filing_id == metadata.filing_id
            for f in repo.facts(job.build_id)
            if type(f.record).__name__ == "FilingMetadata"
        )
        assert store.counts()["events"] == 1


def test_sec_amendment_cross_page_never_appears_before_acceptance(tmp_path):
    current = json.loads(amend_payload())
    current["filings"]["files"] = [
        {
            "name": "CIK0000123456-submissions-001.json",
            "filingFrom": "2026-01-01",
            "filingTo": "2026-01-10",
        }
    ]
    history = json.loads(fixture("submissions.json"))["filings"]["recent"]
    clock = ManualClock(T4 + 1)
    http = FakeHttp({URL: json.dumps(current).encode(), HISTORY_URL: json.dumps(history).encode()})
    source, processor = sec_history(
        (CIK_A,), "EIE research@example.invalid", clock, transport=http, issuer_ciks=(CIK_A,)
    )
    job = BackfillJob(definition(source.spec))
    with (
        SQLiteEventStore(tmp_path / "sec.sqlite") as store,
        FileArchive(tmp_path / "archive") as archive,
    ):
        repo = DatasetRepository(store)
        BackfillCoordinator(repo, archive, ((source, processor),), clock=clock).run(job)
        reader = DatasetReader(repo, job.build_id)
        assert len(reader.filings(CIK_A, T3)) == 1
        after = reader.filings(CIK_A, T4)
        assert len(after) == 2 and after[1].amendment_status is AmendmentStatus.LINKED
        assert after[1].amends_filing_id == after[0].filing_id
        assert len(reader.events_known_at(T3).events) == 1
        assert len(reader.events_known_at(T4).events) == 2


@pytest.mark.parametrize(
    "cursor",
    [
        "bad",
        "{}",
        '{"names":[],"index":0}',
        '{"names":["../x"],"index":0}',
        '{"names":["CIK0000123456-submissions-001.json"],"index":true}',
        '{"names":["CIK0000999999-submissions-001.json"],"index":0}',
    ],
)
def test_sec_cursor_cannot_change_host_path_or_cik(cursor):
    source, _ = sec_history(
        (CIK_A,), "EIE research@example.invalid", ManualClock(T4 + 1), transport=FakeHttp({})
    )
    part = plan(BackfillJob(definition(source.spec)))[0]
    with pytest.raises(ValidationError):
        source.fetch(part, cursor, Event())


@pytest.mark.parametrize(
    "status,exception", [(404, TransportPolicyError), (500, TransientTransportError)]
)
def test_sec_failure_statuses_are_bounded(status, exception):
    http = FakeHttp({URL: HttpResponse(status)})
    source, _ = sec_history(
        (CIK_A,), "EIE research@example.invalid", ManualClock(T4 + 1), transport=http
    )
    with pytest.raises(exception):
        source.fetch(plan(BackfillJob(definition(source.spec)))[0], None, Event())
    assert len(http.requests) <= 3


def test_sec_preflight_identity_pins_and_current_tickers_are_not_historical(tmp_path):
    with pytest.raises(ValidationError):
        resource_cik("cik:../escape")
    with pytest.raises(ValidationError):
        sec_history((), "EIE research@example.invalid", ManualClock(T4))
    source, _ = sec_history(
        (CIK_A,), "EIE research@example.invalid", ManualClock(T4), transport=FakeHttp({})
    )
    with pytest.raises(ValidationError):
        SecHistoricalSource((CIK_A, CIK_A), source.client)
    with pytest.raises(ValidationError):
        SecHistoricalSource((CIK_A,), source.client, issuer_ciks=(CIK.parse(7654321),))
    job = BackfillJob(definition(source.spec))
    with SQLiteEventStore(tmp_path / "s.sqlite") as store, FileArchive(tmp_path / "a") as archive:
        with pytest.raises(ValidationError):
            BackfillCoordinator(
                DatasetRepository(store), archive, ((source, SecHistoryProcessor((CIK_A,))),)
            ).prepare(job)
