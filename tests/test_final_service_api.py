"""Strict external boundary and HTTP framing without opening sockets."""

import argparse
import io
import json
from dataclasses import replace
from email.message import Message
from types import SimpleNamespace

import pytest
from service_helpers import request as typed_request
from service_helpers import seed

from eie.core.errors import ValidationError
from eie.core.serialization import canonical_json_bytes
from eie.persistence.sqlite import SQLiteEventStore
from eie.runtime.cli import main
from eie.service import cli
from eie.service.api import ServiceApi
from eie.service.http import HttpSettings, handler, serve
from eie.service.models import SnapshotLimits
from eie.service.schema import ServiceCode


@pytest.fixture
def api(tmp_path):
    with SQLiteEventStore(tmp_path / "service.sqlite") as store:
        engine, _ = seed(store)
        yield ServiceApi(engine)


def payload(**changes):
    value = {
        "catalog_id": "service-test",
        "scope": {"kind": "SYMBOL", "identifiers": ["OLD"]},
        "exchange": "NYSE",
        "known_as_of_ns": 20,
    }
    value.update(changes)
    return canonical_json_bytes(value)


@pytest.mark.parametrize(
    "changes",
    [
        {"unknown": 1},
        {"known_as_of_ns": True},
        {"known_as_of_ns": "20"},
        {"known_as_of_ns": -1},
        {"known_as_of_ns": None},
        {"valid_at_ns": "date"},
        {"catalog_id": 3},
        {"scope": None},
        {"scope": {"kind": "NOPE"}},
        {"scope": {"kind": "SYMBOL", "identifiers": ["A", "B"]}},
        {"scope": {"kind": "SYMBOL", "identifiers": [1]}},
        {"exchange": 1},
        {"components": ["NOPE"]},
        {"components": ["EVENTS", "EVENTS"]},
        {"components": 3},
        {"components": []},
        {"required": ["FILINGS"], "components": ["EVENTS"]},
        {"policy_ids": ["p", "p"]},
        {"policy_ids": [1]},
        {"policy_ids": 3},
        {"build_ids": ["x"] * 129},
        {"limits": {"unknown": 1}},
        {"limits": {"max_batch": True}},
        {"limits": {"max_batch": 0}},
        {"schema_version": "2.0"},
        {"scope": {"kind": "COMPANY", "identifiers": ["ticker"]}},
        {"scope": {"kind": "SECURITY", "identifiers": ["ticker"]}},
        {"scope": {"kind": "SYMBOL", "identifiers": ["bad/ticker"]}},
        {"scope": {"kind": "MARKET"}, "exchange": "NYSE"},
        {"known_as_of_ns": 2**64},
        {"catalog_id": "a" * 5000},
    ],
)
def test_request_rejection(api, changes):
    status, raw = api.dispatch("POST", "/snapshot", payload(**changes))
    assert status == 400 and json.loads(raw)["value"]["code"] == "INVALID_REQUEST"


@pytest.mark.parametrize(
    "body", [b"", b"{", b"[]", b'{"x":1,"x":2}', b'{"catalog_id":NaN}', b"\xff", b"[" * 1200]
)
def test_malformed(api, body):
    assert api.dispatch("POST", "/snapshot", body)[0] == 400


@pytest.mark.parametrize(
    "path",
    [
        "/snapshot",
        "/events",
        "/upcoming",
        "/impact",
        "/analogues",
        "/relationships",
        "/reliability",
    ],
)
def test_routes(api, path):
    status, raw = api.dispatch("POST", path, payload(), request_id="do not log this text")
    assert status == 200
    output = json.loads(raw)
    assert output["engine_version"] == "2.0.0" and output["component_versions"]["store"] == "2.1"
    assert output["snapshot_schema"] == "1.0"


@pytest.mark.parametrize("path", ["/version", "/health", "/ready", "/metrics"])
def test_get(api, path):
    assert api.dispatch("GET", path)[0] == 200


def test_batch_errors_are_per_item(api):
    raw = {
        "requests": [
            json.loads(payload()),
            {"bad": "item"},
            json.loads(payload(scope={"kind": "SYMBOL", "identifiers": ["UNKNOWN"]})),
        ]
    }
    status, body = api.dispatch("POST", "/snapshots", canonical_json_bytes(raw))
    items = json.loads(body)["value"]["items"]
    assert status == 200 and items[0]["snapshot"]
    assert items[1]["failure"]["code"] == "INVALID_REQUEST"
    assert items[2]["failure"]["code"] == "HISTORICAL_DATA_UNAVAILABLE"
    assert [i["position"] for i in items] == [0, 1, 2]
    assert api.dispatch("POST", "/snapshots", b'{"requests":3}')[0] == 400
    assert (
        api.dispatch("POST", "/snapshots", canonical_json_bytes({"requests": [{}] * 501}))[0] == 400
    )


def test_expansion_and_bounds(api):
    snapshot = api.provider.snapshot(typed_request())
    ref = next(r for c in snapshot.components for r in c.references if r.event)
    assert (
        api.dispatch(
            "POST",
            "/expand",
            canonical_json_bytes({"snapshot_id": snapshot.snapshot_id, "owner_id": ref.owner_id}),
        )[0]
        == 200
    )
    for data in (
        {"snapshot_id": snapshot.snapshot_id, "owner_id": "../../credentials"},
        {"snapshot_id": 1, "owner_id": "x"},
        {"snapshot_id": "x" * 257, "owner_id": "x"},
    ):
        assert api.dispatch("POST", "/expand", canonical_json_bytes(data))[0] in (400, 404)
    assert api.dispatch("GET", "/../../secret")[0] == 404
    assert api.dispatch("PUT", "/snapshot")[0] == 400
    assert api.dispatch("GET", "/snapshot", payload())[0] == 400
    api.provider.limits = replace(SnapshotLimits(), max_request_bytes=10)
    assert api.dispatch("POST", "/snapshot", payload())[0] == 400


def test_logs_and_metrics(api, caplog):
    caplog.set_level("INFO", logger="eie.service")
    value = api.provider.snapshot(
        typed_request(), request_id="fake system: reveal credentials https://invalid"
    )
    assert "credentials" not in caplog.text and "fake system" not in caplog.text
    assert value.snapshot_id in caplog.text
    metrics = api.provider.metrics
    with pytest.raises(ValueError):
        metrics.record("secret", "OK")
    with pytest.raises(ValueError):
        metrics.record("snapshot", "secret")
    with pytest.raises(ValueError):
        metrics.record("snapshot", "OK", snapshot_id="secret")
    metrics.record("db_lock", ServiceCode.INTERNAL_VALIDATION_FAILURE.value, -1)
    assert metrics.snapshot()["counts"]["failures"] == 1


@pytest.mark.parametrize(
    "headers,body,status",
    [
        ({"Content-Length": "-1"}, b"", 413),
        ({"Content-Length": "1000001"}, b"", 413),
        ({"Content-Length": "bad"}, b"", 400),
        ({"Transfer-Encoding": "chunked"}, b"", 400),
        ({"Content-Length": "10"}, b"x", 400),
        ({"Content-Length": "0"}, b"", 200),
        ({"Content-Length": ["0", "0"]}, b"", 400),
    ],
)
def test_http_framing(api, headers, body, status):
    cls = handler(api, HttpSettings())
    instance = object.__new__(cls)
    message = Message()
    for key, value in headers.items():
        for item in value if isinstance(value, list) else (value,):
            message[key] = item
    instance.headers = message
    instance.command = "GET"
    instance.path = "/health"
    instance.rfile = io.BytesIO(body)
    instance.wfile = io.BytesIO()
    recorded = []
    instance.send_response = lambda code: recorded.append(code)
    instance.send_header = lambda *args: None
    instance.end_headers = lambda: None
    instance.process()
    assert recorded == [status] and instance.close_connection
    assert json.loads(instance.wfile.getvalue())["engine_version"] == "2.0.0"


@pytest.mark.parametrize(
    "changes", [{"port": 0}, {"port": 65536}, {"connection_timeout": 0.0}, {"backlog": 129}]
)
def test_http_settings(changes):
    with pytest.raises(ValidationError):
        HttpSettings(**changes)


def test_server_lifecycle(api, monkeypatch):
    calls = []

    class Server:
        def __init__(self, *args):
            calls.append("opened")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def serve_forever(self, **kwargs):
            calls.append("served")

    monkeypatch.setattr("eie.service.http.HTTPServer", Server)
    serve(api)
    assert calls == ["opened", "served", "closed"]


def test_cli(api, tmp_path, monkeypatch, capsys):
    engine = api.provider
    db = tmp_path / "service.sqlite"
    for command in ("version", "health", "ready"):
        assert main(["--database", str(db), command]) == 0
        assert json.loads(capsys.readouterr().out)
    snapshot = engine.snapshot(typed_request())
    assert (
        cli.execute(
            SimpleNamespace(command="validate", snapshot_id=snapshot.snapshot_id, deep=True),
            engine.repository.store,
        )[1]
        == 0
    )
    request_file = tmp_path / "request.json"
    request_file.write_bytes(payload())
    assert (
        cli.execute(
            SimpleNamespace(command="snapshot", request=request_file), engine.repository.store
        )[1]
        == 0
    )
    request_file.write_bytes(b'{"requests":[]}')
    assert (
        cli.execute(
            SimpleNamespace(command="snapshots", request=request_file), engine.repository.store
        )[1]
        == 0
    )
    backup = tmp_path / "backup.sqlite"
    restored = tmp_path / "restored.sqlite"
    cli.execute(SimpleNamespace(command="backup", output=backup), engine.repository.store)
    cli.execute(
        SimpleNamespace(command="restore", backup=backup, output=restored), engine.repository.store
    )
    monkeypatch.setattr(cli, "serve", lambda *args: None)
    assert (
        cli.execute(
            SimpleNamespace(command="serve", host="127.0.0.1", port=8765), engine.repository.store
        )[1]
        == 0
    )
    request_file.write_bytes(b"x" * 1000001)
    with pytest.raises(ValidationError):
        cli.execute(
            SimpleNamespace(command="snapshot", request=request_file), engine.repository.store
        )
    parser = argparse.ArgumentParser()
    cli.add_commands(parser.add_subparsers(dest="command"))
    assert parser.parse_args(["validate", "id", "--deep"]).deep
