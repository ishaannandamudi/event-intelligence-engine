"""Historical CLI plan is pure; run/resume/query/rebuild remain offline."""

import json

import pytest
from historical_helpers import fixture

from eie.persistence import codec
from eie.runtime.cli import main


def files(tmp_path):
    job, source, processor = fixture()
    definition = tmp_path / "definition.json"
    definition.write_bytes(codec.dumps(job.definition))
    pages = tmp_path / "pages.json"
    rows = [
        {
            "provider": source.spec.provider_id,
            "resource": key[0],
            "start_ns": key[1],
            "end_ns": key[2],
            "cursor": key[3],
            "body_hex": value.body.hex(),
            "source_reference": value.source_reference,
            "next_cursor": value.next_cursor,
            "coverage": value.coverage.value,
            "too_many_results": value.too_many_results,
        }
        for key, value in source._pages.items()
    ]
    pages.write_text(json.dumps(rows))
    return job, definition, pages, rows


def test_plan_does_not_create_database_archive_or_fetch(tmp_path, capsys):
    job, definition, pages, rows = files(tmp_path)
    database = tmp_path / "never-created.sqlite"
    assert (
        main(["--database", str(database), "backfill", "plan", "--definition", str(definition)])
        == 0
    )
    result = json.loads(capsys.readouterr().out)
    assert result["build_id"] == job.build_id and len(result["partitions"]) == 2
    assert not database.exists() and not (tmp_path / "archive").exists()


def test_offline_run_resume_status_manifest_export_validate_query_rebuild(tmp_path, capsys):
    job, definition, pages, rows = files(tmp_path)
    base = ["--database", str(tmp_path / "events.sqlite")]
    archive = ["--archive", str(tmp_path / "archive")]
    assert (
        main(
            base
            + [
                "backfill",
                "run",
                "--definition",
                str(definition),
                "--pages",
                str(pages),
                "--max-pages",
                "1",
            ]
            + archive
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "PAUSED"
    assert main(base + ["backfill", "status", "--build", job.build_id]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PAUSED"
    assert (
        main(
            base + ["backfill", "resume", "--build", job.build_id, "--pages", str(pages)] + archive
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "COMPLETED"
    export = tmp_path / "manifest.json"
    assert (
        main(base + ["dataset", "manifest", "--build", job.build_id, "--output", str(export)]) == 0
    )
    fingerprint = json.loads(capsys.readouterr().out)["content_fingerprint"]
    assert codec.loads(export.read_bytes()).content_fingerprint == fingerprint
    assert main(base + ["dataset", "validate", "--build", job.build_id] + archive) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "VALID"
    assert main(base + ["dataset", "events-as-of", "--build", job.build_id, "--at-ns", "14"]) == 0
    assert len(json.loads(capsys.readouterr().out)["events"]) == 1
    assert (
        main(
            base
            + ["dataset", "rebuild", "--build", job.build_id, "--build-version", "cli-rebuild"]
            + archive
        )
        == 0
    )
    rebuilt = json.loads(capsys.readouterr().out)["build_id"]
    assert main(base + ["dataset", "manifest", "--build", rebuilt]) == 0
    assert json.loads(capsys.readouterr().out)["content_fingerprint"] == fingerprint


@pytest.mark.parametrize(
    "case",
    [
        "definition",
        "too-large",
        "page-fields",
        "media-input",
        "duplicate",
        "resource",
        "hex",
        "typed-fields",
        "unknown",
        "missing-db",
        "no-manifest",
        "live-other",
    ],
)
def test_cli_fails_closed_with_sanitized_diagnostics(tmp_path, capsys, case):
    job, definition, pages, rows = files(tmp_path)
    args = [
        "--database",
        str(tmp_path / "events.sqlite"),
        "backfill",
        "run",
        "--definition",
        str(definition),
        "--pages",
        str(pages),
        "--archive",
        str(tmp_path / "archive"),
    ]
    if case == "definition":
        definition.write_bytes(codec.dumps(()))
    elif case == "too-large":
        definition.write_bytes(b" " * 20_000_001)
    elif case == "page-fields":
        pages.write_text('[{"unexpected":"PRIVATE_MARKER"}]')
    elif case == "media-input":
        pages.write_text("{}")
    elif case == "duplicate":
        pages.write_text(json.dumps(rows + rows))
    elif case == "resource":
        rows[0]["resource"] = "unconfigured"
        pages.write_text(json.dumps(rows))
    elif case == "hex":
        rows[0]["body_hex"] = "not hex"
        pages.write_text(json.dumps(rows))
    elif case == "typed-fields":
        rows[0]["start_ns"] = "wrong"
        pages.write_text(json.dumps(rows))
    elif case in ("unknown", "missing-db"):
        if case == "unknown":
            main(args)
            capsys.readouterr()
        args = [
            "--database",
            str(tmp_path / "events.sqlite"),
            "backfill",
            "status",
            "--build",
            "unknown",
        ]
    elif case == "no-manifest":
        assert main(args + ["--max-pages", "1"]) == 0
        capsys.readouterr()
        args = [
            "--database",
            str(tmp_path / "events.sqlite"),
            "dataset",
            "manifest",
            "--build",
            job.build_id,
        ]
    else:
        args = [
            "--database",
            str(tmp_path / "events.sqlite"),
            "backfill",
            "run",
            "--definition",
            str(definition),
            "--live",
            "--archive",
            str(tmp_path / "archive"),
        ]
    assert main(args) == 2
    output = capsys.readouterr()
    assert "PRIVATE_MARKER" not in output.err and output.out == ""
