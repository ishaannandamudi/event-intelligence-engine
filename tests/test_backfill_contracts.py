"""Pure deterministic plans and strict immutable historical contracts."""

from dataclasses import FrozenInstanceError, replace

import pytest
from historical_helpers import fixture, item, one

from eie.archive.models import ArchiveAcquisition, ArchiveObject
from eie.backfill.models import (
    GapReason,
    GapRecord,
    HistoricalCapability,
    Partition,
    PartitionPolicy,
    PartitionProgress,
    ReconstructionQuality,
    SourceSpec,
    TimeRange,
    cursor_value,
)
from eie.backfill.planner import plan, split
from eie.config.settings import Settings
from eie.core.errors import ValidationError
from eie.datasets.models import (
    BackfillJob,
    CompletenessReport,
    HistoricalFact,
    LineageRecord,
)
from eie.ingestion.models import RetentionPolicy
from eie.persistence import codec
from eie.providers.sec.settings import SecSettings
from eie.sources.models import RawSourceObservation
from eie.sources.trust import SourceClass


def test_deterministic_identity_range_and_partition_coverage():
    job, _, _ = fixture()
    changed_clock = BackfillJob(replace(job.definition, created_at_ns=999))
    assert job.build_id == changed_clock.build_id and job.job_id == changed_clock.job_id
    assert plan(job) == plan(changed_clock)
    assert [(p.interval.start_ns, p.interval.end_ns) for p in plan(job)] == [(10, 15), (15, 20)]
    assert job.definition.interval.contains(10) and not job.definition.interval.contains(20)
    assert codec.loads(codec.dumps(job)) == job
    with pytest.raises(FrozenInstanceError):
        job.build_version = "mutable"
    assert BackfillJob(job.definition, "2").build_id != job.build_id
    assert BackfillJob(job.definition, parent_build_id=job.build_id).build_id != job.build_id


def test_multi_resource_capability_boundaries_and_bounded_plan():
    job, _, _ = one(capability=HistoricalCapability(max_range_ns=3, earliest_ns=12, latest_ns=18))
    spec = replace(job.definition.sources[0], resources=("cik:0000000001", "cik:0000000002"))
    job = BackfillJob(replace(job.definition, sources=(spec,)))
    parts = plan(job)
    assert len(parts) == 8
    for resource in spec.resources:
        selected = [p.interval for p in parts if p.resource_id == resource]
        assert selected == [
            TimeRange(10, 12),
            TimeRange(12, 15),
            TimeRange(15, 18),
            TimeRange(18, 20),
        ]
        assert sum(p.end_ns - p.start_ns for p in selected) == 10
    with pytest.raises(ValidationError):
        plan(BackfillJob(replace(job.definition, partition_policy=PartitionPolicy(3, 7))))
    parent = parts[1]
    children = split(parent, 3)
    assert children[0].interval.end_ns == children[1].interval.start_ns
    assert all(c.parent_id == parent.partition_id and c.depth == 1 for c in children)
    with pytest.raises(ValidationError):
        split(parent, 0)
    with pytest.raises(ValidationError):
        split(replace(parent, interval=TimeRange(1, 2)), 1)


@pytest.mark.parametrize(
    "changes",
    [
        {"max_range_ns": 0},
        {"max_pages": True},
        {"max_items": 100001},
        {"max_payload_bytes": 100000001},
        {"max_split_depth": 17},
        {"max_split_depth": "3"},
        {"earliest_ns": -1},
        {"earliest_ns": 4, "latest_ns": 3},
        {"supported": 1},
        {"point_in_time": "yes"},
        {"assumptions": ("Z", "A")},
    ],
)
def test_capability_rejects_unsafe_bounds(changes):
    with pytest.raises(ValidationError):
        HistoricalCapability(**changes)


@pytest.mark.parametrize(
    "factory",
    [
        lambda: TimeRange(1, 1),
        lambda: TimeRange(-1, 2),
        lambda: TimeRange(True, 2),
        lambda: PartitionPolicy(0),
        lambda: PartitionPolicy(1, 1000001),
        lambda: SourceSpec("bad provider", ("macro",), "1", "1"),
        lambda: SourceSpec("TEST", (), "1", "1"),
        lambda: SourceSpec("TEST", ("b", "a"), "1", "1"),
        lambda: SourceSpec("TEST", ("a",), "1", "1", source_class="OFFICIAL_PRIMARY"),
        lambda: SourceSpec("TEST", ("a",), "1", "1", identity_bindings=(("b", "iss"),)),
        lambda: SourceSpec("TEST", ("a",), "1", "1", identity_bindings=(("a", "a"), ("a", "b"))),
        lambda: Partition("job", "TEST", "a", TimeRange(1, 2), depth=1),
        lambda: Partition("job", "TEST", "a", TimeRange(1, 2), depth=True),
        lambda: PartitionProgress("p", pages=-1),
        lambda: PartitionProgress("p", status="PENDING"),
        lambda: PartitionProgress("p", visited_cursors=["a"]),
        lambda: PartitionProgress("p", visited_cursors=("a", "a")),
        lambda: cursor_value("a" * 32769),
        lambda: GapRecord("p", "TEST", "a", TimeRange(1, 2), "unknown", 3),
        lambda: GapRecord(
            "p", "TEST", "a", TimeRange(1, 2), GapReason.UNKNOWN_COMPLETENESS, 3, retryable=1
        ),
        lambda: ArchiveObject("../escape", 1),
        lambda: ArchiveObject("a" * 64, True),
        lambda: CompletenessReport(2, 1, 0, 0, 0, 0, 0, ()),
        lambda: CompletenessReport(1, 1, 0, 0, 0, 0, 0, (), gap_reasons=(("x", 0),)),
    ],
)
def test_contract_boundaries(factory):
    with pytest.raises(ValidationError):
        factory()


@pytest.mark.parametrize(
    "changes",
    [
        {"namespace": " "},
        {"interval": (10, 20)},
        {"sources": ()},
        {"settings": "settings"},
        {"settings": Settings(sec=SecSettings(user_agent="contact@example.invalid"))},
        {"sources": []},
        {"partition_policy": 1},
        {"archive_policy": 1},
        {"event_families": ("Z", "A")},
        {"schema_version": "99"},
    ],
)
def test_definition_validation(changes):
    job, _, _ = fixture()
    with pytest.raises(ValidationError):
        replace(job.definition, **changes)


def test_historical_fact_cannot_fabricate_local_history():
    row = item(12, "cpi")
    raw = RawSourceObservation(
        row.source, row.item_id, row.item_version, row.fragment_hash, "application/json", 100, 100
    )
    fact = HistoricalFact(
        "SYNTH_HIST",
        "macro",
        row.record.event_id,
        1,
        12,
        row.quality,
        raw,
        replace(row.record, evidence_observation_ids=(raw.observation_id,)),
        row.source.adapter_version,
        "1",
    )
    assert (
        replace(
            fact, raw=replace(raw, source_observed_at_ns=200, ingested_at_ns=200)
        ).content_fingerprint
        == fact.content_fingerprint
    )
    for changes in (
        {"source_revision": True},
        {"source_revision": 2},
        {"source_available_ns": 101},
        {"quality": ReconstructionQuality.DATE_ONLY_RECONSTRUCTED},
        {"source_available_ns": None},
        {"provider_id": "OTHER"},
        {"adapter_version": "wrong"},
        {"record": object()},
        {"quality": "UNKNOWN"},
        {"provenance": "invalid"},
        {"record": replace(fact.record, source_classes=frozenset({SourceClass.OFFICIAL_PRIMARY}))},
        {"record": replace(fact.record, evidence_observation_ids=("obs_" + "b" * 64,))},
    ):
        with pytest.raises(ValidationError):
            replace(fact, **changes)
    assert (
        replace(
            fact, source_available_ns=None, quality=ReconstructionQuality.UNKNOWN
        ).source_available_ns
        is None
    )
    with pytest.raises(ValidationError):
        replace(row, fragment_hash="bad")
    with pytest.raises(ValidationError):
        replace(row, source_revision=0)
    with pytest.raises(ValidationError):
        BackfillJob("bad")
    assert LineageRecord("b", "p", "a", "f", "o", "h", "v", "v", "s").fact_id == "f"
    gap = GapRecord("p", "TEST", "a", TimeRange(1, 2), GapReason.UNKNOWN_COMPLETENESS, 3)
    assert replace(gap, discovered_ns=5, partition_id="other").gap_id == gap.gap_id


@pytest.mark.parametrize(
    "changes",
    [
        {"page_number": 0},
        {"page_number": True},
        {"object": "x"},
        {"retention": "x"},
        {"coverage": "x"},
        {"provenance": "x"},
        {"too_many_results": 1},
        {"replayed_at_ns": 0},
        {"source_reference": "http://example.invalid"},
        {"source_reference": "file:///tmp/x"},
        {"source_reference": "https://u:p@example.invalid/x"},
    ],
)
def test_archive_metadata_validation(changes):
    acq = ArchiveAcquisition(
        "b",
        "p",
        "TEST",
        "r",
        1,
        "https://example.invalid/x",
        1,
        ArchiveObject("a" * 64, 2),
        "application/json",
        RetentionPolicy(),
        "1",
        "1",
    )
    with pytest.raises(ValidationError):
        replace(acq, **changes)
    assert replace(acq, next_cursor="next").next_cursor == "next"
