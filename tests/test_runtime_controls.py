"""Runtime CLI, signals, operational validation and immutable config tests."""

import json
import runpy
import signal
import sys
from dataclasses import replace
from pathlib import Path
from threading import Event, Thread

import pytest

from eie.core.errors import ValidationError
from eie.ingestion.checkpoints import resume_cursor
from eie.ingestion.models import (
    Checkpoint,
    Cursor,
    CursorKind,
    FailureClass,
    Latencies,
    PollResult,
    Quarantine,
    RetentionMode,
    RetentionPermission,
    RetentionPolicy,
    RuntimeHealth,
    RuntimeHealthState,
)
from eie.ingestion.processors import IngestionFailure
from eie.runtime import cli
from eie.runtime.clock import ManualClock, SystemClock
from eie.runtime.config import RuntimeConfig
from eie.runtime.coordinator import IngestionCoordinator
from eie.runtime.shutdown import termination_signals
from eie.runtime.sources import FixtureSource

ROOT = Path(__file__).parent


@pytest.mark.parametrize(
    "provider,fixture",
    (("SEC_EDGAR", "sec/submissions.json"), ("BLS", "official/bls_schedule.json")),
)
def test_cli_fixture_commit_reopen_query_controls(tmp_path, capsys, provider, fixture):
    args = ["--database", str(tmp_path / "runtime.sqlite")]
    assert (
        cli.main(args + ["ingest-once", provider, "--fixture", str(ROOT / "fixtures" / fixture)])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["committed"] and result["raw_created"] > 0
    assert cli.main(args + ["provider-health", provider]) == 0
    assert json.loads(capsys.readouterr().out)["state"] == "HEALTHY"
    resource = "submissions:0000123456" if provider == "SEC_EDGAR" else "release-calendar"
    assert cli.main(args + ["checkpoint", provider, resource]) == 0
    assert json.loads(capsys.readouterr().out)["generation"] == 1
    assert cli.main(args + ["validate-store"]) == 0
    assert json.loads(capsys.readouterr().out)["errors"] == []
    assert (
        cli.main(args + ["ingest-once", provider, "--fixture", str(ROOT / "fixtures" / fixture)])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["duplicates"] > 0
    assert cli.main(args + ["provider-health", "UNKNOWN"]) == 0
    assert json.loads(capsys.readouterr().out) is None
    assert cli.main(args + ["checkpoint", "UNKNOWN", "missing"]) == 0
    assert json.loads(capsys.readouterr().out) is None


def test_cli_explicit_live_gate_errors_and_limits(tmp_path, monkeypatch, capsys):
    args = ["--database", str(tmp_path / "state.sqlite"), "ingest-once", "SEC_EDGAR"]
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert exc.value.code == 2
    monkeypatch.delenv("EIE_SEC_USER_AGENT", raising=False)
    assert cli.main(args + ["--live"]) == 2
    missing = tmp_path / "missing"
    assert cli.main(args + ["--fixture", str(missing)]) == 2
    malformed = tmp_path / "bad.json"
    malformed.write_bytes(b"not JSON")
    assert cli.main(args + ["--fixture", str(malformed)]) == 1
    huge = tmp_path / "large"
    huge.write_bytes(b"x" * 2_000_001)
    assert cli.main(args + ["--fixture", str(huge)]) == 2
    assert "not JSON" not in capsys.readouterr().err


def test_cli_continuous_foreground_shutdown(tmp_path, monkeypatch, capsys):
    def finish(self, timeout=0.5):
        self.request_stop()
        return True

    monkeypatch.setattr(IngestionCoordinator, "wait_for_stop", finish)
    args = [
        "--database",
        str(tmp_path / "state.sqlite"),
        "run",
        "SEC_EDGAR",
        "--fixture",
        str(ROOT / "fixtures/sec/submissions.json"),
    ]
    before = signal.getsignal(signal.SIGTERM)
    assert cli.main(args) == 0
    assert json.loads(capsys.readouterr().out)["graceful"]
    assert signal.getsignal(signal.SIGTERM) == before


def test_module_entry_point_is_explicit(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["eie", "--database", str(tmp_path / "state.sqlite"), "validate-store"]
    )
    with pytest.raises(SystemExit) as exc:
        runpy.run_module("eie", run_name="__main__")
    assert exc.value.code == 0 and json.loads(capsys.readouterr().out)["schema_version"] == "2.1"


def test_signals_restore_and_require_main_thread():
    stopped = []
    before = signal.getsignal(signal.SIGINT)
    with termination_signals(lambda: stopped.append(True)):
        handler = signal.getsignal(signal.SIGINT)
        handler(signal.SIGINT, None)
    assert stopped == [True] and signal.getsignal(signal.SIGINT) == before
    failures = []

    def background():
        try:
            with termination_signals(lambda: None):
                pass
        except ValidationError as exc:
            failures.append(exc)

    thread = Thread(target=background)
    thread.start()
    thread.join(1)
    assert len(failures) == 1


@pytest.mark.parametrize(
    "changes",
    (
        {"enabled": 1},
        {"worker_count": 0},
        {"fetch_count": 33},
        {"queue_capacity": True},
        {"shutdown_timeout": -1},
        {"scheduler_resolution": float("inf")},
    ),
)
def test_runtime_config_validation(changes):
    with pytest.raises(ValidationError):
        RuntimeConfig(**changes)


@pytest.mark.parametrize("kind", tuple(CursorKind))
def test_cursor_roundtrip_kinds(kind):
    value = "123" if kind in (CursorKind.SEQUENCE, CursorKind.TIMESTAMP) else "opaque-source-item"
    if kind is CursorKind.ACCESSION:
        value = "0000123456-26-000001"
    cursor = Cursor(kind, value)
    assert cursor.value == value and value not in repr(cursor)
    assert resume_cursor(None, cursor) == cursor and resume_cursor(cursor, None) == cursor
    assert resume_cursor(cursor, cursor) == cursor


def test_cursor_kind_change_fail_closed():
    with pytest.raises(IngestionFailure):
        resume_cursor(Cursor(CursorKind.SEQUENCE, "1"), Cursor(CursorKind.OPAQUE, "resume"))


@pytest.mark.parametrize(
    "constructor",
    (
        lambda: Cursor("OPAQUE", "x"),
        lambda: Cursor(CursorKind.OPAQUE, "x" * 4097),
        lambda: Cursor(CursorKind.SEQUENCE, "x"),
        lambda: Checkpoint("bad", "recent", 1, 1, 1),
        lambda: Checkpoint("SYNTH_A", "recent", 0, 1, 1),
        lambda: Checkpoint("SYNTH_A", "recent", 1, 1, 1, schema_version="2.0"),
        lambda: Checkpoint("SYNTH_A", "recent", 1, 1, 1, cursor="bad"),
        lambda: Checkpoint("SYNTH_A", "recent", 1, 1, 1, etag="x\ny"),
        lambda: RuntimeHealth("SYNTH_A", 1, "HEALTHY"),
        lambda: RuntimeHealth("SYNTH_A", 1, RuntimeHealthState.UNKNOWN, last_success_ns=2),
        lambda: RuntimeHealth("SYNTH_A", 1, RuntimeHealthState.UNKNOWN, consecutive_failures=True),
        lambda: RuntimeHealth("SYNTH_A", 1, RuntimeHealthState.UNKNOWN, error_category="bad"),
        lambda: PollResult("SYNTH_A", "recent", 2, 1, 200, True),
        lambda: PollResult("SYNTH_A", "recent", 1, 1, 200, True, items_ingested=-1),
        lambda: PollResult("SYNTH_A", "recent", 1, 1, 200, 1),
        lambda: PollResult("SYNTH_A", "recent", 1, 1, True, True),
        lambda: Quarantine("SYNTH_A", "recent", "x", "v1", FailureClass.PARSE_FAILURE, 1, 1),
        lambda: Quarantine(
            "SYNTH_A", "recent", "a" * 64, "v1", FailureClass.PARSE_FAILURE, 1, 1, True
        ),
        lambda: RetentionPolicy("bad"),
        lambda: RetentionPolicy(max_raw_bytes=0),
        lambda: RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.UNKNOWN),
        lambda: RetentionPolicy(RetentionMode.BOUNDED_RAW, RetentionPermission.RESTRICTED),
        lambda: FixtureSource("SYNTH_A", "recent", ()),
        lambda: Latencies(fetch_ns=-1),
    ),
)
def test_operational_contracts_fail_closed(constructor):
    with pytest.raises(ValidationError):
        constructor()


def test_operational_fingerprint_and_clocks():
    cp = Checkpoint("SYNTH_A", "recent", 1, 1, 1)
    assert cp.result_fingerprint != replace(cp, generation=2).result_fingerprint
    assert SystemClock().utc_ns() > 0 and SystemClock().monotonic_ns() > 0
    stop = Event()
    stop.set()
    assert SystemClock().wait(0, stop)
    manual = ManualClock()
    assert manual.wait(1, stop) and manual.utc_ns() == 1
    with pytest.raises(ValidationError):
        manual.wait(-1, stop)
    with pytest.raises(ValidationError):
        manual.advance(-1)
