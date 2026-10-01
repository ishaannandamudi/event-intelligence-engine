"""Ordered transactional SQLite schema, independent from quantitative schemas."""

import sqlite3

from eie.core.errors import PersistenceError

STORE_SCHEMA_VERSION = "1.0"
MIGRATIONS: tuple[tuple[str, ...], ...] = (
    (
        "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, schema_version TEXT NOT NULL)",
        "CREATE TABLE observations(id TEXT PRIMARY KEY, provider TEXT NOT NULL, item TEXT "
        "NOT NULL, available INTEGER NOT NULL, payload BLOB NOT NULL)",
        "CREATE INDEX observations_provider_item ON observations(provider,item)",
        "CREATE TABLE events(id TEXT NOT NULL, revision INTEGER NOT NULL, available INTEGER"
        " NOT NULL, payload BLOB NOT NULL, PRIMARY KEY(id,revision))",
        "CREATE INDEX events_available ON events(available,id)",
        "CREATE TABLE evidence(event_id TEXT NOT NULL, revision INTEGER NOT NULL, "
        "observation_id TEXT NOT NULL REFERENCES observations(id), PRIMARY "
        "KEY(event_id,revision,observation_id), FOREIGN KEY(event_id,revision) REFERENCES "
        "events(id,revision))",
        "CREATE TABLE domain_records(id TEXT PRIMARY KEY, kind TEXT NOT NULL, available "
        "INTEGER, issuer TEXT, event_id TEXT, measurement TEXT, accession TEXT, payload "
        "BLOB NOT NULL)",
        "CREATE INDEX domain_series ON domain_records(kind,event_id,measurement,available)",
        "CREATE INDEX domain_issuer ON domain_records(issuer,available)",
        "CREATE INDEX domain_accession ON domain_records(accession)",
        "CREATE TABLE domain_evidence(record_id TEXT NOT NULL REFERENCES "
        "domain_records(id), observation_id TEXT NOT NULL REFERENCES observations(id), "
        "PRIMARY KEY(record_id,observation_id))",
        "CREATE TABLE sessions(namespace TEXT PRIMARY KEY, layout_version TEXT NOT NULL, "
        "payload BLOB NOT NULL)",
        "CREATE TABLE checkpoints(provider TEXT NOT NULL, resource TEXT NOT NULL, "
        "generation INTEGER NOT NULL, payload BLOB NOT NULL, PRIMARY "
        "KEY(provider,resource))",
        "CREATE TABLE operations(id INTEGER PRIMARY KEY, provider TEXT NOT NULL, available "
        "INTEGER NOT NULL, kind TEXT NOT NULL, payload BLOB NOT NULL)",
        "CREATE INDEX operations_provider ON operations(provider,available)",
        "CREATE TABLE quarantine(provider TEXT NOT NULL, resource TEXT NOT NULL, hash TEXT "
        "NOT NULL, adapter_version TEXT NOT NULL, payload BLOB NOT NULL, PRIMARY "
        "KEY(provider,resource,hash,adapter_version))",
        "CREATE TABLE acquisitions(id TEXT PRIMARY KEY, provider TEXT NOT NULL, resource "
        "TEXT NOT NULL, observed INTEGER NOT NULL, payload_hash TEXT NOT NULL, metadata "
        "BLOB NOT NULL, raw BLOB)",
    ),
)


def migrate(connection: sqlite3.Connection) -> None:
    """Refuse unknown or partial schemas; failed migrations leave no partial DDL."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        tables = {
            str(r[0])
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        if tables and "schema_migrations" not in tables:
            raise PersistenceError("unversioned database refused")
        rows = (
            []
            if not tables
            else list(
                connection.execute(
                    "SELECT version,schema_version FROM schema_migrations ORDER BY version"
                )
            )
        )
        if rows != [(i + 1, STORE_SCHEMA_VERSION) for i in range(len(rows))] or len(rows) > len(
            MIGRATIONS
        ):
            raise PersistenceError("unsupported database migration version")
        for index in range(len(rows), len(MIGRATIONS)):
            for statement in MIGRATIONS[index]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?,?)", (index + 1, STORE_SCHEMA_VERSION)
            )
        expected = {
            statement.split()[2].split("(")[0]: "".join(statement.split()).casefold()
            for migration in MIGRATIONS
            for statement in migration
            if statement.startswith(("CREATE TABLE ", "CREATE INDEX "))
        }
        actual = {
            name: "".join(sql.split()).casefold()
            for name, sql in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }
        if actual != expected:
            raise PersistenceError("incomplete or incompatible database schema")
        connection.commit()
    except (sqlite3.Error, PersistenceError) as exc:
        connection.rollback()
        raise PersistenceError("SQLite migration failed") from exc
