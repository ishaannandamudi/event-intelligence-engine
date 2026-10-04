"""Operator-only consistent SQLite backup/restore; immutable exclusive outputs."""

import os
import sqlite3
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from eie.core.errors import PersistenceError, ValidationError
from eie.persistence import codec
from eie.persistence.migrations import MIGRATION_VERSIONS, MIGRATIONS, STORE_SCHEMA_VERSION
from eie.persistence.sqlite import SQLiteEventStore
from eie.service import owners
from eie.service.models import BackupManifest, EIESnapshot, Publication
from eie.service.repository import ServiceRepository
from eie.service.schema import OwnerKind


def destination(path: Path) -> None:
    if path.exists() or path.is_symlink() or path.parent.is_symlink() or not path.parent.is_dir():
        raise ValidationError("new trusted backup/restore destination required")


def inspect(connection: sqlite3.Connection) -> tuple[int, int]:
    if list(connection.execute("PRAGMA integrity_check")) != [("ok",)] or list(
        connection.execute("PRAGMA foreign_key_check")
    ):
        raise PersistenceError("backup physical integrity failure")
    versions = list(
        connection.execute("SELECT version,schema_version FROM schema_migrations ORDER BY version")
    )
    if versions != list(enumerate(MIGRATION_VERSIONS, 1)):
        raise PersistenceError("backup schema/version unsupported")
    expected = {
        s.split()[2].split("(")[0]: "".join(s.split()).casefold()
        for m in MIGRATIONS
        for s in m
        if s.startswith(("CREATE TABLE ", "CREATE INDEX ", "CREATE TRIGGER "))
    }
    actual = {
        n: "".join(s.split()).casefold()
        for n, s in connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
        )
    }
    if actual != expected:
        raise PersistenceError("backup DDL unsupported")
    for table, cls in (("service_snapshots", EIESnapshot), ("service_publications", Publication)):
        for identity, fp, payload in connection.execute(
            "SELECT id,fingerprint,payload FROM " + table
        ):
            value = codec.loads(payload)
            if (
                not isinstance(value, cls)
                or value.artifact_id != identity
                or value.fingerprint != fp
            ):
                raise PersistenceError("backup typed artifact corrupt")
    for row in connection.execute("SELECT * FROM service_publications"):
        publication = codec.loads(row[-1])
        if not isinstance(publication, Publication) or tuple(row[:-1]) != ServiceRepository.indices(
            publication
        ):
            raise PersistenceError("backup publication index mismatch")
        ref = publication.reference
        args: tuple[object, ...]
        if ref.kind is OwnerKind.EVENT:
            sql, args = (
                "SELECT payload FROM events WHERE id=? AND revision=?",
                (ref.lookup[0], int(ref.lookup[1])),
            )
        else:
            key = "build_id" if ref.kind is OwnerKind.DATASET else "id"
            sql, args = (
                "SELECT payload FROM " + owners.TABLES[ref.kind] + " WHERE " + key + "=?",
                ref.lookup,
            )
        native = connection.execute(sql, args).fetchone()
        if native is None or sha256(native[0]).hexdigest() != ref.storage_fingerprint:
            raise PersistenceError("backup owner reference corrupt")
        members = list(
            connection.execute(
                "SELECT kind,identifier FROM service_members WHERE publication=? "
                "ORDER BY kind,identifier",
                (publication.artifact_id,),
            )
        )
        expected_members = sorted(
            (scope.kind.value, identity)
            for scope in publication.scopes
            for identity in (scope.identifiers or ("",))
        )
        if members != expected_members:
            raise PersistenceError("backup membership corrupt")
    for _identity, catalog, known in connection.execute(
        "SELECT id,catalog,known FROM service_snapshots"
    ):
        if not connection.execute(
            "SELECT 1 FROM service_seals WHERE catalog=? AND known=?", (catalog, known)
        ).fetchone():
            raise PersistenceError("backup snapshot seal missing")
    return tuple(
        connection.execute(
            "SELECT (SELECT COUNT(*) FROM service_snapshots),(SELECT COUNT(*) FROM "
            "service_publications)"
        ).fetchone()
    )


def manifest(path: Path, counts: tuple[int, int]) -> BackupManifest:
    digest = sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1_000_000), b""):
            digest.update(chunk)
    return BackupManifest(digest.hexdigest(), STORE_SCHEMA_VERSION, *counts)


def backup(store: SQLiteEventStore, path: Path) -> BackupManifest:
    destination(path)
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".eie-backup-", delete=False) as stream:
            temporary = Path(stream.name)
        with store._lock:
            store._check()
            if store._connection.in_transaction:
                raise PersistenceError("backup requires no active calling transaction")
            target = sqlite3.connect(temporary)
            try:
                store._connection.backup(target)
                counts = inspect(target)
            finally:
                target.close()
        os.link(temporary, path)
        return manifest(path, counts)
    except sqlite3.Error as exc:
        raise PersistenceError("backup failed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def restore(source: Path, path: Path) -> BackupManifest:
    destination(path)
    if source.is_symlink() or not source.is_file():
        raise ValidationError("trusted regular backup file required")
    temporary: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, prefix=".eie-restore-", delete=False) as stream:
            temporary = Path(stream.name)
            with source.open("rb") as incoming:
                for chunk in iter(lambda: incoming.read(1_000_000), b""):
                    stream.write(chunk)
        from urllib.parse import quote

        connection = sqlite3.connect("file:" + quote(str(temporary)) + "?mode=ro", uri=True)
        try:
            counts = inspect(connection)
        finally:
            connection.close()
        os.link(temporary, path)
        return manifest(path, counts)
    except sqlite3.Error as exc:
        raise PersistenceError("restore failed") from exc
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
