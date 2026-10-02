from dataclasses import replace
from hashlib import sha256

import pytest

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.events.identity import EventIdentity
from eie.events.lifecycle import LifecycleState
from eie.events.models import StructuredFact
from eie.events.scope import EventScope, ScopeKind
from eie.events.taxonomy import EventFamily, EventType
from eie.studies.contamination import ContaminationIndex
from eie.studies.engine import StudyEngine
from eie.studies.market import IndexedMarketProvider
from eie.studies.models import (
    AnchorQuality,
    AnchorSemantics,
    CoarsePolicy,
    EventAnchor,
    SessionPolicy,
    StudyQuality,
)
from eie.studies.windows import event_anchor
from examples.event_studies import DAY, MINUTE, market
from examples.historical_rebuild import item
from tests.study_helpers import inputs_history, window


def record(at, key="earnings", revision=1):
    v = item(at, key, source_revision=revision)
    e = replace(
        v.record,
        identity=EventIdentity(
            EventType(EventFamily.EARNINGS, "EARNINGS.RELEASE"),
            EventScope(ScopeKind.COMPANY, ("issuer_fixture",)),
            key,
        ),
        published_at_ns=at,
    )
    return replace(v, record=e, fragment_hash=sha256(canonical_json_bytes(e)).hexdigest())


def test_original_revision_is_not_replaced_by_later_correction(tmp_path):
    at = 9 * DAY + 10 * MINUTE
    original = record(at)
    corrected = record(at + 20 * MINUTE, revision=2)
    store, reader = inputs_history(tmp_path, (original, corrected))
    try:
        e = original.record.event_id
        before = StudyEngine(reader, IndexedMarketProvider((market(),)), at + 5 * MINUTE)
        r = before.study_event(
            e,
            "security_fixture",
            before.anchor(e, AnchorSemantics.PUBLICATION),
            window(),
            data_as_of_ns=at + 5 * MINUTE,
        )
        assert r.definition.event.revision.number == 1 and r.anchor_ns == at
        later = StudyEngine(reader, IndexedMarketProvider((market(),)), at + 25 * MINUTE)
        assert later.anchor(e, AnchorSemantics.PUBLICATION).timestamp_ns == at + 20 * MINUTE
        assert later._events[e].revision.number == 2
        assert (
            before.study_event(
                e,
                "security_fixture",
                before.anchor(e, AnchorSemantics.PUBLICATION),
                window(),
                data_as_of_ns=at + 5 * MINUTE,
            )
            == r
        )
    finally:
        store.close()


@pytest.mark.parametrize(
    "quality,timing,coarse",
    [
        (AnchorQuality.SESSION_LABEL, "BEFORE_MARKET", CoarsePolicy.SESSION_OPEN),
        (AnchorQuality.SESSION_LABEL, "AFTER_MARKET", CoarsePolicy.SESSION_CLOSE),
        (AnchorQuality.DATE_ONLY, "UNKNOWN", CoarsePolicy.SESSION_OPEN),
    ],
)
def test_coarse_bmo_amc_date_policy_preserves_source_precision(tmp_path, quality, timing, coarse):
    v = record(8 * DAY)
    label = "1970-01-10"
    precision = "DATE_ONLY" if quality is AnchorQuality.DATE_ONLY else "DATE_PLUS_SESSION"
    facts = tuple(
        sorted(
            (
                StructuredFact("release_timing", timing),
                StructuredFact("schedule_precision", precision),
                StructuredFact("scheduled_date", label),
            ),
            key=lambda f: f.name,
        )
    )
    v = replace(
        v,
        record=replace(
            v.record, published_at_ns=None, lifecycle=LifecycleState.SCHEDULED, facts=facts
        ),
    )
    store, reader = inputs_history(tmp_path, (v,))
    try:
        data = market()
        data = replace(
            data,
            sessions=tuple(replace(s, label=label) if s.label == "9" else s for s in data.sessions),
        )
        at = 9 * DAY if coarse is CoarsePolicy.SESSION_OPEN else 9 * DAY + 30 * MINUTE
        engine = StudyEngine(reader, IndexedMarketProvider((data,)), at + 5 * MINUTE)
        anchor = EventAnchor(AnchorSemantics.SCHEDULED, quality, session_label=label)
        w = replace(window(), anchor_semantics=AnchorSemantics.SCHEDULED, coarse_policy=coarse)
        r = engine.study_event(
            v.record.event_id, "security_fixture", anchor, w, data_as_of_ns=at + 5 * MINUTE
        )
        assert r.anchor_ns == at and r.definition.anchor.quality is quality
        assert StudyQuality.COARSE_EVENT_TIME in r.quality
        assert r.definition.anchor.timestamp_ns is None
        with pytest.raises(ValidationError):
            engine.study_event(
                v.record.event_id,
                "security_fixture",
                anchor,
                replace(w, coarse_policy=CoarsePolicy.REJECT),
                data_as_of_ns=at + 5 * MINUTE,
            )
        with pytest.raises(ValidationError):
            engine.study_event(
                v.record.event_id,
                "security_fixture",
                replace(anchor, session_label="bad"),
                w,
                data_as_of_ns=at + 5 * MINUTE,
            )
        wrong = (
            AnchorQuality.DATE_ONLY
            if quality is AnchorQuality.SESSION_LABEL
            else AnchorQuality.SESSION_LABEL
        )
        with pytest.raises(ValidationError):
            engine.study_event(
                v.record.event_id,
                "security_fixture",
                replace(anchor, quality=wrong),
                w,
                data_as_of_ns=at + 5 * MINUTE,
            )
        if timing != "UNKNOWN":
            opposite = (
                CoarsePolicy.SESSION_CLOSE
                if coarse is CoarsePolicy.SESSION_OPEN
                else CoarsePolicy.SESSION_OPEN
            )
            with pytest.raises(ValidationError):
                engine.study_event(
                    v.record.event_id,
                    "security_fixture",
                    anchor,
                    replace(w, coarse_policy=opposite),
                    data_as_of_ns=at + 5 * MINUTE,
                )
        with pytest.raises(ValidationError):
            event_anchor(v.record, AnchorSemantics.SCHEDULED)
    finally:
        store.close()


def test_supplied_trading_sessions_and_closed_anchor(tmp_path):
    at = 9 * DAY + 30 * MINUTE
    v = record(at)
    store, reader = inputs_history(tmp_path, (v,))
    try:
        engine = StudyEngine(reader, IndexedMarketProvider((market(),)), at)
        anchor = engine.anchor(v.record.event_id, AnchorSemantics.PUBLICATION)
        w = replace(window(), session_policy=SessionPolicy.TRADING)
        r = engine.study_event(
            v.record.event_id, "security_fixture", anchor, w, data_as_of_ns=10 * DAY + 5 * MINUTE
        )
        assert r.post_end_ns == 10 * DAY + 5 * MINUTE
        assert StudyQuality.MARKET_CLOSED in r.quality
        assert r.reference_ns == at
        assert r.post_return is not None
        assert all(p.target_ns >= 10 * DAY for p in r.curve if p.offset_ns > 0)
        no_calendar = replace(market(), sessions=())
        with pytest.raises(ValidationError):
            StudyEngine(reader, IndexedMarketProvider((no_calendar,)), at).study_event(
                v.record.event_id,
                "security_fixture",
                anchor,
                w,
                data_as_of_ns=10 * DAY + 5 * MINUTE,
            )
    finally:
        store.close()


def test_contamination_same_bundle_macro_scheduled_and_order_independence():
    at = 9 * DAY + 10 * MINUTE
    primary = record(at).record
    same_bundle = replace(
        primary,
        identity=replace(
            primary.identity,
            semantic_key="dividend",
            event_type=EventType(EventFamily.CORPORATE_ACTION, "CORPORATE_ACTION.DIVIDEND"),
        ),
        scheduled_for_ns=at,
    )
    macro = replace(
        primary,
        identity=EventIdentity(
            EventType(EventFamily.MACRO, "MACRO.CPI.US"), EventScope(ScopeKind.MACRO_GLOBAL), "cpi"
        ),
        published_at_ns=at + MINUTE,
        revision=replace(primary.revision, available_from_ns=at + MINUTE),
    )
    unrelated = replace(
        primary,
        identity=replace(
            primary.identity, scope=EventScope(ScopeKind.COMPANY, ("other",)), semantic_key="other"
        ),
        published_at_ns=at + 2 * MINUTE,
        revision=replace(primary.revision, available_from_ns=at + 2 * MINUTE),
        evidence_observation_ids=("obs_" + "b" * 64,),
    )
    unknown = replace(
        unrelated,
        identity=replace(unrelated.identity, semantic_key="unknown"),
        published_at_ns=None,
        effective_at_ns=None,
    )
    events = (primary, same_bundle, macro, unrelated, unknown)
    index = ContaminationIndex(events)
    flags = index.flags(primary, at, at - MINUTE, at + 3 * MINUTE)
    assert flags == ContaminationIndex(tuple(reversed(events))).flags(
        primary, at, at - MINUTE, at + 3 * MINUTE
    )
    assert index.unknown_count == 1 and len(flags) == 2
    assert {"SOURCE_BUNDLE", "SAME_ENTITY_EVENT", "CO_OCCURRENCE", "SCHEDULED_OVERLAP"} <= set(
        flags[0].reasons
    )
    assert {"MACRO_OVERLAP", "MARKET_WIDE_OVERLAP"} <= set(flags[1].reasons)
    assert index.flags(primary, at, at + 4 * MINUTE, at + 5 * MINUTE) == ()


def test_cancelled_event_and_reconstruction_policy_fail_closed(tmp_path):
    from eie.events.revisions import EventRevision, RevisionKind

    at = 9 * DAY + 10 * MINUTE
    v = record(at)
    cancelled = replace(
        v,
        source_revision=2,
        source_available_ns=at + 1,
        range_at_ns=at + 1,
        record=replace(
            v.record,
            lifecycle=LifecycleState.CANCELLED,
            revision=EventRevision(2, RevisionKind.CANCELLATION, at + 1, 1),
        ),
    )
    store, reader = inputs_history(tmp_path, (v, cancelled))
    try:
        engine = StudyEngine(reader, IndexedMarketProvider((market(),)), at + 5 * MINUTE)
        with pytest.raises(ValidationError):
            engine.study_event(
                v.record.event_id,
                "security_fixture",
                engine.anchor(v.record.event_id, AnchorSemantics.PUBLICATION),
                window(),
                data_as_of_ns=at + 5 * MINUTE,
            )
    finally:
        store.close()


def test_native_sec_acceptance_anchor_against_linked_historical_filing(tmp_path):
    from test_sec_historical import FakeHttp, definition
    from test_sec_ingestion import CIK_A, HISTORY_URL, T4, URL, fixture

    from eie.archive.storage import FileArchive
    from eie.backfill.coordinator import BackfillCoordinator
    from eie.backfill.models import ReconstructionQuality
    from eie.backfill.sec import sec_history
    from eie.datasets.models import BackfillJob
    from eie.datasets.queries import DatasetReader
    from eie.datasets.repository import DatasetRepository
    from eie.persistence.sqlite import SQLiteEventStore
    from eie.runtime.clock import ManualClock
    from eie.studies.market import MarketBar, MarketSession, MarketSlice
    from eie.studies.models import StudyPolicy

    clock = ManualClock(T4 + 1)
    source, processor = sec_history(
        (CIK_A,),
        "EIE research@example.invalid",
        clock,
        transport=FakeHttp(
            {URL: fixture("submissions.json"), HISTORY_URL: fixture("history.json")}
        ),
        issuer_ciks=(CIK_A,),
    )
    job = BackfillJob(definition(source.spec))
    with (
        SQLiteEventStore(tmp_path / "sec.sqlite") as store,
        FileArchive(tmp_path / "archive") as archive,
    ):
        repo = DatasetRepository(store)
        BackfillCoordinator(repo, archive, ((source, processor),), clock=clock).run(job)
        reader = DatasetReader(repo, job.build_id)
        event = reader.events_known_at(T4 + 1).events[0]
        at = event.effective_at_ns
        template = market()
        bars = tuple(
            MarketBar(
                "security_fixture",
                at + i * MINUTE,
                at + (i + 1) * MINUTE,
                100,
                100,
                100,
                100,
                10,
                at + (i + 1) * MINUTE,
                "SYNTHETIC",
                template.identity.timeframe,
            )
            for i in range(-5, 6)
        )
        data = MarketSlice(
            template.identity,
            bars,
            sessions=(
                MarketSession(
                    "synthetic-sec-window", at - 5 * MINUTE, at + 6 * MINUTE, 0, "SYNTHETIC", "1"
                ),
            ),
        )
        engine = StudyEngine(reader, IndexedMarketProvider((data,)), T4 + 1)
        anchor = engine.anchor(event.event_id, AnchorSemantics.ACCEPTANCE)
        assert anchor.timestamp_ns == at
        w = replace(window(), anchor_semantics=AnchorSemantics.ACCEPTANCE)
        r = engine.study_event(
            event.event_id,
            "security_fixture",
            anchor,
            w,
            data_as_of_ns=max(T4 + 1, at + 5 * MINUTE),
        )
        assert (
            r.post_return == 0
            and r.definition.reconstruction_quality
            is ReconstructionQuality.SOURCE_TIMESTAMP_RECONSTRUCTED
        )
        assert StudyQuality.EVENT_HISTORY_INCOMPLETE in r.quality
        with pytest.raises(ValidationError):
            engine.study_event(
                event.event_id,
                "security_fixture",
                anchor,
                w,
                data_as_of_ns=max(T4 + 1, at + 5 * MINUTE),
                policy=StudyPolicy(
                    required_quality=ReconstructionQuality.AUTHORITATIVE_POINT_IN_TIME
                ),
            )
