"""Fail closed on archive writes, malformed extracts and invalid linked state."""

from dataclasses import replace
from threading import Event

import pytest
from historical_helpers import item, one, page, workspace

from eie.backfill.models import GapReason, JobStatus
from eie.backfill.planner import plan
from eie.backfill.rebuild import ArchiveHistoricalSource
from eie.backfill.sources import (
    FixtureHistoricalSource,
    HistoricalPage,
    logical_identity,
)
from eie.core.errors import PersistenceError, ValidationError
from eie.datasets.models import BackfillJob, ValidationStatus
from eie.datasets.queries import DatasetReader
from eie.datasets.validation import validate_dataset
from eie.persistence import codec
from eie.providers.common.errors import ProviderParseError, ProviderSchemaMismatchError
from eie.sources.trust import SourceClass
from eie.transport.errors import AcquisitionCancelled


def test_failed_archive_write_does_not_create_valid_reference(tmp_path, monkeypatch):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):

        def fail(*args):
            raise PersistenceError("synthetic storage failure")

        monkeypatch.setattr(archive, "write", fail)
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert (
            repo.acquisitions(job.build_id) == ()
            and repo.manifest(job.build_id).archive_hashes == ()
        )
        assert repo.gaps(job.build_id)[0].reason is GapReason.ARCHIVE_FAILURE
        assert (
            validate_dataset(repo, archive, job.build_id).status
            is ValidationStatus.VALID_WITH_KNOWN_GAPS
        )


def test_global_persistence_fault_is_failed_and_can_resume(tmp_path):
    job, source, processor = one()

    def fault(name):
        if name == "after_data":
            raise PersistenceError("synthetic commit failure")

    with workspace(tmp_path, ((source, processor),), fault=fault) as (repo, archive, runtime):
        with pytest.raises(PersistenceError):
            runtime.run(job)
        assert repo.status(job.build_id) is JobStatus.FAILED and repo.facts(job.build_id) == ()
        assert repo.partitions(job.build_id)[0][1].pages == 0
        runtime.fault = None
        assert runtime.run(job) is JobStatus.COMPLETED


@pytest.mark.parametrize("stop", [True, False])
def test_acquisition_cancellation_is_durable_and_provider_scoped(tmp_path, stop):
    job, source, processor = one()
    runtime = None

    def fetch(part, cursor, event):
        if stop:
            runtime.request_pause()
        raise AcquisitionCancelled("offline cancellation")

    source.fetch = fetch
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, active):
        runtime = active
        assert runtime.run(job) is (JobStatus.PAUSED if stop else JobStatus.COMPLETED_WITH_GAPS)
        if stop:
            assert runtime.process_page(job, plan(job)[0]) == ()


@pytest.mark.parametrize(
    "case",
    [
        "extract-type",
        "input-type",
        "provider",
        "classes",
        "context",
        "media",
        "schema",
        "bounds",
        "untrusted-facts",
    ],
)
def test_untrusted_normalized_or_extract_context_becomes_explicit_gap(tmp_path, case):
    row = item(12, "original")
    payload = page(row)
    if case == "extract-type":
        payload = replace(payload, body=codec.dumps([]))
    elif case == "input-type":
        payload = replace(payload, body=codec.dumps(("wrong",)))
    elif case == "provider":
        payload = page(replace(row, source=replace(row.source, name="OTHER")))
    elif case == "classes":
        payload = page(
            replace(
                row,
                record=replace(
                    row.record, source_classes=frozenset({SourceClass.OFFICIAL_PRIMARY})
                ),
            )
        )
    elif case == "media":
        payload = replace(payload, content_type="text/plain")
    elif case == "bounds":
        payload = page(replace(row, range_at_ns=20))
    job, source, processor = one({("macro", 10, 20, None): payload})
    if case == "schema":

        def schema(*args):
            raise ProviderSchemaMismatchError("synthetic schema drift")

        processor.normalize = schema
    elif case == "untrusted-facts":
        prior = processor.normalize
        processor.normalize = lambda *args: tuple(
            replace(f, resource_id="OTHER") for f in prior(*args)
        )
    elif case == "context":
        # A source class claim from the processor cannot override configured trust.
        source.spec = replace(source.spec, source_class=SourceClass.OFFICIAL_PRIMARY)
        job = BackfillJob(replace(job.definition, sources=(source.spec,)))
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        status = runtime.run(job)
        if case == "bounds":
            assert status is JobStatus.COMPLETED and repo.facts(job.build_id) == ()
        else:
            assert status is JobStatus.COMPLETED_WITH_GAPS
            assert repo.facts(job.build_id) == ()
        if case == "schema":
            assert repo.gaps(job.build_id)[0].reason is GapReason.SCHEMA_MISMATCH


def test_progress_cannot_advance_without_durable_page_and_identities_cannot_collide(tmp_path):
    job, source, processor = one()
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.prepare(job)
        part = plan(job)[0]
        with repo.store.transaction(), pytest.raises(PersistenceError):
            repo.update_progress(
                job.build_id, replace(repo.progress(part.partition_id), pages=1), 0
            )
        with repo.store.transaction(), pytest.raises(PersistenceError):
            repo.update_progress(
                job.build_id, replace(repo.progress(part.partition_id), items_seen=1), 0
            )
        with pytest.raises(PersistenceError):
            DatasetReader(repo, job.build_id)
        assert runtime.run(job) is JobStatus.COMPLETED


def test_fixture_and_archive_stop_and_missing_chain_fail_closed(tmp_path):
    job, source, processor = one()
    stopped = Event()
    stopped.set()
    with pytest.raises(AcquisitionCancelled):
        source.fetch(plan(job)[0], None, stopped)
    missing = FixtureHistoricalSource(source.spec, {})
    with pytest.raises(ValidationError):
        missing.fetch(plan(job)[0], None, Event())
    with workspace(tmp_path) as (repo, archive, _):
        archived = ArchiveHistoricalSource(source.spec, archive, {})
        with pytest.raises(ValidationError):
            archived.fetch(plan(job)[0], None, stopped)
        with pytest.raises(PersistenceError):
            archived.fetch(plan(job)[0], None, Event())
    with pytest.raises(ValidationError):
        logical_identity(object())
    with pytest.raises(ValidationError):
        HistoricalPage("wrong", "https://example.invalid")
    with pytest.raises(ValidationError):
        HistoricalPage(b"bytes", "https://example.invalid", too_many_results=1)


@pytest.mark.parametrize(
    "error,reason",
    [
        (ProviderSchemaMismatchError("schema"), GapReason.SCHEMA_MISMATCH),
        (ProviderParseError("parse"), GapReason.PARSE_FAILURE),
    ],
)
def test_source_reference_parsing_failure_keeps_its_precise_gap_category(tmp_path, error, reason):
    job, source, processor = one({("macro", 10, 20, None): error})
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert repo.gaps(job.build_id)[0].reason is reason
        assert repo.acquisitions(job.build_id) == ()
