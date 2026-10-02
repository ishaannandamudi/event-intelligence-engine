"""Source knowledge cutoffs, later discovery, revisions and historical identity."""

from dataclasses import replace
from hashlib import sha256

import pytest
from expectations_helpers import MACRO_PERIOD, ingest, record, seeded
from historical_helpers import item, one, page, workspace

from eie.backfill.models import GapReason, JobStatus, ReconstructionQuality
from eie.core.errors import PersistenceError
from eie.core.serialization import canonical_json_bytes
from eie.datasets.models import BackfillJob, HistoricalInput
from eie.datasets.queries import DatasetReader
from eie.entities.models import Issuer, Listing, Security, issuer_id, security_id
from eie.entities.protocols import ResolutionState
from eie.events.lifecycle import LifecycleState
from eie.events.revisions import RevisionKind
from eie.events.scope import ApplicabilityQuery
from eie.persistence import codec
from eie.scheduled.models import ScheduleClaim, ScheduleRevision
from eie.sources.models import SourceIdentity
from eie.sources.trust import SourceClass


def audited(record, at, key, revision=1, *, source=None):
    source = source or item(12, "seed").source
    return HistoricalInput(
        source,
        key,
        str(revision),
        sha256(codec.dumps(record)).hexdigest(),
        at,
        at,
        ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME,
        revision,
        record,
    )


def test_later_revision_discovered_first_never_leaks_backwards_and_old_build_is_frozen(tmp_path):
    late, early = (
        item(17, "shared", source_revision=2, title="Revised"),
        item(12, "shared", title="Original"),
    )
    pages = {
        ("macro", 10, 20, None): page(late, cursor="older"),
        ("macro", 10, 20, "older"): page(early),
    }
    job, source, processor = one(pages)
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        reader = DatasetReader(repo, job.build_id)
        assert reader.events_known_at(11).events == ()
        assert reader.events_known_at(12).events[0].title == "Original"
        assert reader.events_known_at(16).events[0].revision.number == 1
        assert reader.events_known_at(17).events[0].revision.number == 2
        assert len(reader.records(17)) == 2
        assert all(
            f.raw.ingested_at_ns == 1000 and f.raw.source_observed_at_ns == 1000
            for f in repo.facts(job.build_id)
        )
        assert all(f.record.ingested_at_ns == 1000 for f in repo.facts(job.build_id))
        assert reader.events_known_at(17).events[0].ingested_at_ns is None
        assert reader.events_known_at(17).events[0].source_observed_at_ns is None
        saved = canonical_json_bytes(reader.events_known_at(12))
        other = BackfillJob(job.definition, "later-discovery")
        source._pages[("macro", 10, 20, None)] = page(
            early, late, item(18, "shared", source_revision=3, title="Correction")
        )
        runtime.run(other)
        assert (
            DatasetReader(repo, other.build_id).events_known_at(18).events[0].revision.number == 3
        )
        assert canonical_json_bytes(reader.events_known_at(12)) == saved
        assert len(reader.events_known_at(17, ApplicabilityQuery(country="CA")).events) == 0
        assert len(reader.events_known_at(17, start_ns=17, end_ns=18).events) == 1
        assert reader.events_known_at(17, start_ns=18).events == ()
        with pytest.raises(PersistenceError):
            reader.events_known_at(17, start_ns=20, end_ns=10)
        assert reader.records(17, provider="OTHER") == ()
        assert reader.records(17, resource="OTHER") == ()


def test_conflicting_revision_and_missing_original_remain_explicit(tmp_path):
    job, source, processor = one(
        {
            ("macro", 10, 20, None): page(
                item(12, "same", title="A"),
                item(12, "same", title="B"),
                item(17, "missing", source_revision=2),
            )
        }
    )
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert GapReason.TEMPORAL_AMBIGUITY in {g.reason for g in repo.gaps(job.build_id)}
        assert len(repo.facts(job.build_id)) == 3
        with pytest.raises(PersistenceError):
            DatasetReader(repo, job.build_id).events_known_at(12)


def test_coarse_history_never_invents_exact_point_in_time_visibility(tmp_path):
    row = replace(
        item(12, "coarse"),
        source_available_ns=None,
        quality=ReconstructionQuality.DATE_ONLY_RECONSTRUCTED,
    )
    job, source, processor = one({("macro", 10, 20, None): page(row)})
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert DatasetReader(repo, job.build_id).records(1000) == ()
        assert len(repo.facts(job.build_id)) == 1
        assert dict(repo.manifest(job.build_id).completeness.reconstruction_quality) == {
            "DATE_ONLY_RECONSTRUCTED": 1
        }


def test_schedule_revisions_keep_source_claim_times_and_no_future_schedule(tmp_path):
    rows = []
    for number, at, scheduled in ((1, 12, 100), (2, 17, 110)):
        original = item(at, "scheduled", source_revision=number)
        event = replace(
            original.record,
            lifecycle=LifecycleState.SCHEDULED if number == 1 else LifecycleState.RESCHEDULED,
            effective_at_ns=None,
            scheduled_for_ns=scheduled,
            revision=replace(
                original.record.revision,
                kind=RevisionKind.ORIGINAL if number == 1 else RevisionKind.RESCHEDULE,
            ),
        )
        claim = ScheduleClaim(
            event.evidence_observation_ids[0], SourceClass.UNVERIFIED, scheduled, at
        )
        schedule = ScheduleRevision(event, (claim,), claim.observation_id)
        rows.append(audited(schedule, at, "schedule", number))
        rows.append(replace(original, record=event))
    job, source, processor = one({("macro", 10, 20, None): page(*rows)})
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        reader = DatasetReader(repo, job.build_id)
        assert reader.events_known_at(16).events[0].scheduled_for_ns == 100
        assert reader.events_known_at(17).events[0].scheduled_for_ns == 110
        assert (
            reader.records(16, kind="ScheduleRevision", latest=True)[
                0
            ].record.event.scheduled_for_ns
            == 100
        )


def test_estimate_vintages_and_macro_actual_corrections_do_not_use_later_values(tmp_path):
    service, (event_id,) = seeded(periods=(MACRO_PERIOD,), macro=True)
    from eie.expectations.models import CPI_HEADLINE_MOM

    for at, value in ((12, 0.3), (17, 0.5)):
        ingest(
            service,
            [
                record(
                    event_id,
                    value,
                    measurement=CPI_HEADLINE_MOM,
                    period=MACRO_PERIOD,
                    unit={"kind": "PERCENT"},
                    vintage=at,
                )
            ],
            at,
        )
    first = service.engine.estimates(event_id, CPI_HEADLINE_MOM, 12).observations[0]
    second = max(
        service.engine.estimates(event_id, CPI_HEADLINE_MOM, 17).observations,
        key=lambda row: row.revision.available_from_ns,
    )
    source = SourceIdentity(
        "SYNTH_A", "audited-estimates", "historical-contract-1", SourceClass.LICENSED_NEWS
    )
    rows = [
        audited(first, 12, "estimate", first.revision.number, source=source),
        audited(second, 17, "estimate", second.revision.number, source=source),
    ]
    actual_service, _ = seeded(periods=(MACRO_PERIOD,), macro=True)
    for at, value in ((13, 0.4), (18, 0.6)):
        ingest(
            actual_service,
            [
                record(
                    event_id,
                    value,
                    measurement=CPI_HEADLINE_MOM,
                    period=MACRO_PERIOD,
                    kind="ACTUAL",
                    unit={"kind": "PERCENT"},
                    vintage=None,
                    published_at_ns=at,
                    record_id="actual",
                )
            ],
            at,
        )
    first_actual = actual_service.engine.actuals(event_id, CPI_HEADLINE_MOM, 13).observations[0]
    second_actual = max(
        actual_service.engine.actuals(event_id, CPI_HEADLINE_MOM, 18).observations,
        key=lambda row: row.revision.available_from_ns,
    )
    rows += [
        audited(first_actual, 13, "actual", first_actual.revision.number, source=source),
        audited(second_actual, 18, "actual", second_actual.revision.number, source=source),
    ]
    job, source_fixture, processor = one({("macro", 10, 20, None): page(*rows)})
    source_fixture.spec = replace(
        source_fixture.spec, provider_id="SYNTH_A", source_class=SourceClass.LICENSED_NEWS
    )
    job = BackfillJob(replace(job.definition, sources=(source_fixture.spec,)))
    with workspace(tmp_path, ((source_fixture, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        reader = DatasetReader(repo, job.build_id)
        assert (
            reader.records(16, kind="EstimateObservation", latest=True)[0].record.value.value == 0.3
        )
        assert (
            reader.records(17, kind="EstimateObservation", latest=True)[0].record.value.value == 0.5
        )
        assert (
            reader.records(17, kind="ActualObservation", latest=True)[0].record.value.value == 0.4
        )
        assert (
            reader.records(18, kind="ActualObservation", latest=True)[0].record.value.value == 0.6
        )
        for fact in repo.facts(job.build_id):
            assert fact.record.source.ingested_at_ns == 1000
        invalid = replace(rows[1], source_available_ns=11)
        source_fixture._pages[("macro", 10, 20, None)] = page(invalid)
        bad = BackfillJob(job.definition, "bad-vintage")
        assert runtime.run(bad) is JobStatus.COMPLETED_WITH_GAPS


def test_historical_listing_valid_time_ticker_reuse_and_resolution(tmp_path):
    rows = []
    ids = []
    for index, available, start, end in ((1, 12, 1, 15), (2, 17, 15, None)):
        issuer = Issuer(issuer_id("REGISTRY", str(index)), f"Issuer {index}")
        security = Security(security_id("REGISTRY", str(index)), issuer.issuer_id, "Common")
        listing = Listing(security.security_id, "REUSED", "NYSE", start, end, available)
        ids.append(security.security_id)
        for entity_record in (issuer, security, listing):
            rows.append(
                audited(entity_record, available, f"{type(entity_record).__name__}:{index}")
            )
    job, source, processor = one({("macro", 10, 20, None): page(*rows)})
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        reader = DatasetReader(repo, job.build_id)
        assert reader.registry(12).resolve("REUSED", 12, 14).candidate_ids == (ids[0],)
        assert reader.registry(16).resolve("REUSED", 16).state is ResolutionState.UNRESOLVED
        assert reader.registry(17).resolve("REUSED", 17).candidate_ids == (ids[1],)
        assert reader.registry(17).resolve("REUSED", 17, 14).candidate_ids == (ids[0],)
        assert reader.registry(11).resolve("REUSED", 11).state is ResolutionState.UNRESOLVED


def test_multi_evidence_interchange_is_rejected_without_collapsing_provenance(tmp_path):
    row = item(12, "many")
    row = replace(
        row,
        record=replace(row.record, evidence_observation_ids=("obs_" + "a" * 64, "obs_" + "b" * 64)),
    )
    job, source, processor = one({("macro", 10, 20, None): page(row)})
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED_WITH_GAPS
        assert repo.facts(job.build_id) == ()


@pytest.mark.parametrize(
    "families,universe,count", [(("MACRO",), ("US",), 1), (("EARNINGS",), (), 0), ((), ("CA",), 0)]
)
def test_definition_family_and_universe_filter_canonical_records(
    tmp_path, families, universe, count
):
    job, source, processor = one()
    job = BackfillJob(replace(job.definition, event_families=families, entity_universe=universe))
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        runtime.run(job)
        assert len(DatasetReader(repo, job.build_id).events_known_at(15).events) == count
        assert len(repo.acquisitions(job.build_id)) == 1


def test_identical_event_from_two_resources_preserves_one_logical_event(tmp_path):
    row = item(12, "shared")
    job, source, processor = one()
    source.spec = replace(source.spec, resources=("first", "second"))
    source._pages = {(resource, 10, 20, None): page(row) for resource in source.spec.resources}
    job = BackfillJob(replace(job.definition, sources=(source.spec,)))
    with workspace(tmp_path, ((source, processor),)) as (repo, archive, runtime):
        assert runtime.run(job) is JobStatus.COMPLETED
        reader = DatasetReader(repo, job.build_id)
        assert len(reader.events_known_at(12).events) == 1
        assert len(reader.records(12, resource="second")) == 1
        assert dict(repo.manifest(job.build_id).record_counts)["CanonicalEvents"] == 1
        assert dict(repo.manifest(job.build_id).record_counts)["RawSourceObservations"] == 1
