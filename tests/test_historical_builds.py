"""Real SQLite page atomicity, bounded scheduling and durable resume."""

import sqlite3
from dataclasses import replace

import pytest
from historical_helpers import fixture, item, one, page, workspace

from eie.backfill.coordinator import BackfillConfig
from eie.backfill.models import (
    CoverageClaim,
    GapReason,
    HistoricalCapability,
    JobStatus,
    PartitionPolicy,
    PartitionStatus,
)
from eie.backfill.planner import plan
from eie.backfill.rebuild import rebuild
from eie.backfill.sources import FixtureHistoricalSource, HistoricalPage
from eie.core.errors import PersistenceError
from eie.datasets.models import BackfillJob, ValidationStatus
from eie.datasets.validation import validate_dataset
from eie.persistence import codec, migrations
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.clock import ManualClock
from eie.transport.errors import TransientTransportError, TransportPolicyError


class Crash(BaseException):
    pass


@pytest.mark.parametrize(
    "boundary",
    [
        "after_fetch",
        "after_archive",
        "after_normalize",
        "after_data",
        "before_progress",
        "before_commit",
        "after_commit",
    ],
)
def test_crash_restart_preserves_atomic_data_and_progress(tmp_path, boundary):
    job, source, processor = fixture()

    def crash(name):
        if name == boundary:
            raise Crash()

    with workspace(tmp_path, ((source, processor),), fault=crash) as (repo, archive, runtime):
        with pytest.raises(Crash):
            runtime.run(job)
        parts = repo.partitions(job.build_id)
        completed = sum(state.pages for _, state in parts)
        assert completed == (1 if boundary == "after_commit" else 0)
        assert len(repo.facts(job.build_id)) == completed
        assert len(repo.acquisitions(job.build_id)) == completed
        assert repo.manifest(job.build_id) is None
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        assert len(repo.facts(job.build_id)) == 2
        assert validate_dataset(repo, archive, job.build_id).status is ValidationStatus.VALID


def test_durable_budget_pause_resume_idempotent_finalized_run(tmp_path):
    job, source, processor = fixture()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job, max_pages=1) is JobStatus.PAUSED
        first = repo.acquisitions(job.build_id)
        assert len(first) == 1
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        job = BackfillJob(replace(job.definition, created_at_ns=123))
        assert runtime.run(job) is JobStatus.COMPLETED
        assert repo.acquisitions(job.build_id)[:1] == first or first[0] in repo.acquisitions(
            job.build_id
        )
        assert source.requests == 2
        manifest = repo.manifest(job.build_id)
        assert runtime.run(job) is JobStatus.COMPLETED and source.requests == 2
        assert manifest == repo.manifest(job.build_id)
        with pytest.raises(PersistenceError):
            repo.transition(job.build_id, JobStatus.RUNNING)
        with pytest.raises(sqlite3.IntegrityError):
            repo.store._connection.execute(
                "DELETE FROM historical_facts WHERE build_id=?", (job.build_id,)
            )
        with pytest.raises(sqlite3.IntegrityError):
            repo.store._connection.execute("UPDATE dataset_manifests SET payload=payload")


@pytest.mark.parametrize("cancel", [False, True])
def test_requested_pause_and_cancel_stop_new_admission(tmp_path, cancel):
    job, source, processor = fixture()

    def stop(name):
        if name == "after_fetch":
            runtime.request_cancel() if cancel else runtime.request_pause()

    with workspace(tmp_path, ((source, processor),), fault=stop) as (repo, archive, runtime):
        assert runtime.run(job) is (JobStatus.CANCELLED if cancel else JobStatus.PAUSED)
        assert source.requests == 1 and len(repo.facts(job.build_id)) == 1
        assert repo.manifest(job.build_id) is None
        runtime.fault = None
        if cancel:
            with pytest.raises(PersistenceError):
                runtime.run(job)
        else:
            assert runtime.run(job) is JobStatus.COMPLETED


@pytest.mark.parametrize("cursors", [("a", "a"), ("a", "b", "a"), ("a", "b", "c")])
def test_pagination_loops_and_nonadvancing_bodies_are_archived_gaps(tmp_path, cursors):
    pages, previous = {}, None
    for i, cursor in enumerate(cursors):
        # Third case reuses bytes with distinct advancing cursors.
        row = item(12, "same" if cursors == ("a", "b", "c") else str(i))
        pages[("macro", 10, 20, previous)] = page(row, cursor=cursor)
        previous = cursor
    job, source, processor = one(pages)
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert {g.reason for g in repo.gaps(job.build_id)} == {GapReason.PAGINATION_FAILURE}
        assert repo.failed_acquisitions(job.build_id)
        assert (
            validate_dataset(repo, archive, job.build_id).status
            is ValidationStatus.VALID_WITH_KNOWN_GAPS
        )
        rebuilt = rebuild(
            repo,
            archive,
            job.build_id,
            {"SYNTH_HIST": processor},
            "replay",
            clock=ManualClock(2000),
        )
        assert (
            repo.manifest(rebuilt.build_id).content_fingerprint
            == repo.manifest(job.build_id).content_fingerprint
        )


def test_overlapping_final_page_dedupes_and_preserves_lineage(tmp_path):
    row = item(12, "same")
    pages = {("macro", 10, 20, None): page(row, cursor="a"), ("macro", 10, 20, "a"): page(row)}
    job, source, processor = one(pages)
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        assert len(repo.facts(job.build_id)) == 1 and len(repo.lineage(job.build_id)) == 2
        state = repo.partitions(job.build_id)[0][1]
        assert (state.pages, state.items_seen, state.items_committed, state.duplicates) == (
            2,
            2,
            1,
            1,
        )
        assert len(repo.manifest(job.build_id).archive_hashes) == 1


def test_dynamic_split_lineage_and_same_version_rebuild(tmp_path):
    pages = {
        ("macro", 10, 20, None): page(split=True),
        ("macro", 10, 15, None): page(item(12, "a")),
        ("macro", 15, 20, None): page(item(17, "b")),
    }
    job, source, processor = one(pages)
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        parts = repo.partitions(job.build_id)
        assert (
            len(parts) == 3
            and sum(state.status is PartitionStatus.SPLIT for _, state in parts) == 1
        )
        assert repo.manifest(job.build_id).completeness.requested_partitions == 2
        rebuilt = rebuild(
            repo,
            archive,
            job.build_id,
            {"SYNTH_HIST": processor},
            "split-replay",
            clock=ManualClock(2000),
        )
        assert (
            repo.manifest(rebuilt.build_id).content_fingerprint
            == repo.manifest(job.build_id).content_fingerprint
        )
        assert validate_dataset(repo, archive, rebuilt.build_id).status is ValidationStatus.VALID


@pytest.mark.parametrize(
    "capability,coverage,reason",
    [
        (
            HistoricalCapability(supported=False),
            CoverageClaim.EXHAUSTIVE,
            GapReason.UNSUPPORTED_HISTORY,
        ),
        (
            HistoricalCapability(point_in_time=False),
            CoverageClaim.EXHAUSTIVE,
            GapReason.NO_POINT_IN_TIME_HISTORY,
        ),
        (HistoricalCapability(), CoverageClaim.UNKNOWN, GapReason.UNKNOWN_COMPLETENESS),
        (HistoricalCapability(), CoverageClaim.PARTIAL, GapReason.PARTIAL_PROVIDER_COVERAGE),
        (HistoricalCapability(), CoverageClaim.UNSUPPORTED, GapReason.UNSUPPORTED_HISTORY),
    ],
)
def test_unknown_partial_unsupported_are_never_known_empty(tmp_path, capability, coverage, reason):
    job, source, processor = one(
        {("macro", 10, 20, None): page(coverage=coverage)}, capability=capability
    )
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert reason in {g.reason for g in repo.gaps(job.build_id)}
        assert repo.manifest(job.build_id).completeness.known_empty_partitions == 0
        if not capability.supported or not capability.point_in_time:
            assert source.requests == 0
        rebuilt = rebuild(
            repo, archive, job.build_id, {"SYNTH_HIST": processor}, "gaps", clock=ManualClock(2000)
        )
        assert (
            repo.manifest(rebuilt.build_id).content_fingerprint
            == repo.manifest(job.build_id).content_fingerprint
        )


def test_confirmed_empty_is_complete_and_reproducible(tmp_path):
    job, source, processor = one({("macro", 10, 20, None): page()})
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        assert repo.partitions(job.build_id)[0][1].status is PartitionStatus.KNOWN_EMPTY
        assert repo.manifest(job.build_id).completeness.known_empty_partitions == 1


@pytest.mark.parametrize(
    "case",
    ["pages", "payload", "items", "split-depth", "split-count", "parse", "transport", "unexpected"],
)
def test_bounded_failures_remain_qualified_builds(tmp_path, case):
    cap = HistoricalCapability(max_range_ns=10)
    policy = PartitionPolicy(10)
    payload = page(item(12, "a"))
    pages = {("macro", 10, 20, None): payload}
    if case == "pages":
        cap = replace(cap, max_pages=1)
        pages = {("macro", 10, 20, None): page(item(12, "a"), cursor="next")}
    elif case == "payload":
        cap = replace(cap, max_payload_bytes=1)
    elif case == "items":
        cap = replace(cap, max_items=1)
        pages = {("macro", 10, 20, None): page(item(12, "a"), item(13, "b"))}
    elif case.startswith("split"):
        pages = {("macro", 10, 20, None): page(split=True)}
        cap = replace(cap, max_split_depth=0) if case == "split-depth" else cap
        policy = PartitionPolicy(10, 1) if case == "split-count" else policy
    elif case == "parse":
        pages = {
            ("macro", 10, 20, None): HistoricalPage(b"broken", "https://example.invalid/history")
        }
    elif case == "transport":
        pages = {("macro", 10, 20, None): TransportPolicyError("unavailable")}
    else:
        pages = {("macro", 10, 20, None): RuntimeError("synthetic provider failure")}
    job, source, processor = one(pages, capability=cap, policy=policy)
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert repo.gaps(job.build_id) and source.requests <= 2
        assert (
            validate_dataset(repo, archive, job.build_id).status
            is ValidationStatus.VALID_WITH_KNOWN_GAPS
        )
        rebuilt = rebuild(
            repo,
            archive,
            job.build_id,
            {"SYNTH_HIST": processor},
            "failure-replay",
            clock=ManualClock(2000),
        )
        assert (
            repo.manifest(rebuilt.build_id).content_fingerprint
            == repo.manifest(job.build_id).content_fingerprint
        )


def test_transient_failure_exhaustion_and_explicit_resume(tmp_path):
    job, source, processor = one({("macro", 10, 20, None): TransientTransportError("offline")})
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.FAILED and source.requests == 3
        assert repo.manifest(job.build_id) is None
        source._pages[("macro", 10, 20, None)] = page(item(12, "recovered"))
        assert runtime.run(job) is JobStatus.COMPLETED and source.requests == 4
        assert repo.gaps(job.build_id) == ()
        assert repo.partitions(job.build_id)[0][1].attempts == 4


def test_provider_isolation_fairness_and_bounded_workers(tmp_path):
    job, first, processor = fixture()
    second_spec = replace(first.spec, provider_id="SECOND")
    second = FixtureHistoricalSource(
        second_spec, {("macro", start, start + 5, None): page() for start in (10, 15)}
    )
    first._pages = {key: TransportPolicyError("unavailable") for key in first._pages}
    job = BackfillJob(
        replace(
            job.definition,
            sources=tuple(sorted((first.spec, second_spec), key=lambda s: s.provider_id)),
        )
    )
    order = []
    for source in (first, second):
        original = source.fetch

        def tracked(part, cursor, stop, original=original):
            order.append(part.provider_id)
            return original(part, cursor, stop)

        source.fetch = tracked
    with workspace(tmp_path, ((first, processor), (second, processor))) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert order == ["SECOND", "SYNTH_HIST", "SECOND", "SYNTH_HIST"]
        assert (
            sum(p.status is PartitionStatus.KNOWN_EMPTY for _, p in repo.partitions(job.build_id))
            == 2
        )
    parallel = BackfillJob(job.definition, "parallel")
    with workspace(
        tmp_path, ((first, processor), (second, processor)), config=BackfillConfig(2)
    ) as (repo, archive, runtime):
        assert runtime.run(parallel) is JobStatus.COMPLETED_WITH_GAPS
        assert len(repo.partitions(parallel.build_id)) == 4


def test_linked_transaction_requirement_and_missing_plan(tmp_path):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.prepare(job)
        part = plan(job)[0]
        with pytest.raises(PersistenceError):
            repo.update_progress(job.build_id, repo.progress(part.partition_id), 0)
        with pytest.raises(PersistenceError):
            repo.add_partition(job.build_id, part)
        with repo.store.transaction(), pytest.raises(PersistenceError):
            repo.update_progress("wrong", repo.progress(part.partition_id), 0)
        with pytest.raises(PersistenceError):
            repo.progress("unknown")
        with pytest.raises(PersistenceError):
            repo.status("unknown")
        assert validate_dataset(repo, archive, job.build_id).errors == ("MISSING_MANIFEST",)
        assert runtime.process_page(job, part) == ()
        assert runtime.process_page(job, part) == ()


def test_old_database_migration_and_rollback_preserve_live_records(
    tmp_path, raw, event, monkeypatch
):
    path = tmp_path / "old.sqlite"
    conn = sqlite3.connect(path)
    for sql in migrations.MIGRATIONS[0]:
        conn.execute(sql)
    conn.execute("INSERT INTO schema_migrations VALUES (1,'1.0')")
    conn.execute(
        "INSERT INTO observations VALUES (?,?,?,?,?)",
        (
            raw.observation_id,
            raw.source.name,
            raw.source_item_id,
            raw.ingested_at_ns,
            codec.dumps(raw),
        ),
    )
    conn.commit()
    conn.close()
    old = migrations.MIGRATIONS
    monkeypatch.setattr(migrations, "MIGRATIONS", (old[0], (*old[1], "INVALID SQL")))
    with pytest.raises(PersistenceError):
        SQLiteEventStore(path)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT * FROM schema_migrations").fetchall() == [(1, "1.0")]
    assert conn.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
    assert (
        conn.execute("SELECT count(*) FROM sqlite_master WHERE name='historical_jobs'").fetchone()[
            0
        ]
        == 0
    )
    conn.close()
    monkeypatch.setattr(migrations, "MIGRATIONS", old)
    with SQLiteEventStore(path) as store:
        assert store.schema_version == "1.6" and store.get_observation(raw.observation_id) == raw
        store.append_event(event)
        assert store.get_revisions(event.event_id) == (event,)
        assert store.validate() == ()


def test_duplicates_in_another_partition_do_not_certify_known_empty(tmp_path):
    row = item(12, "overlapping-key")
    pages = {
        ("macro", 10, 15, None): page(row),
        ("macro", 15, 20, None): page(replace(row, range_at_ns=17)),
    }
    job, source, processor = fixture()
    source._pages = pages
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        assert all(
            state.status is PartitionStatus.COMPLETED for _, state in repo.partitions(job.build_id)
        )
        assert sum(state.duplicates for _, state in repo.partitions(job.build_id)) == 1
        assert repo.manifest(job.build_id).completeness.known_empty_partitions == 0
