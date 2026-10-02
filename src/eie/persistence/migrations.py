"""Ordered transactional SQLite schema, independent from quantitative schemas."""

import sqlite3

from eie.core.errors import PersistenceError

STORE_SCHEMA_VERSION = "1.2"
MIGRATION_VERSIONS = ("1.0", "1.1", "1.2")
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
    (
        "CREATE TABLE historical_jobs(build_id TEXT PRIMARY KEY,job_id TEXT NOT"
        " NULL UNIQUE,status TEXT NOT NULL,payload BLOB NOT NULL)",
        "CREATE TABLE historical_partitions(id TEXT PRIMARY KEY,build_id TEXT N"
        "OT NULL REFERENCES historical_jobs(build_id),payload BLOB NOT NULL,pro"
        "gress BLOB NOT NULL)",
        "CREATE INDEX historical_partition_build ON historical_partitions(build_id,id)",
        "CREATE TABLE archive_objects(hash TEXT PRIMARY KEY,payload BLOB NOT NULL)",
        "CREATE TABLE historical_acquisitions(id TEXT PRIMARY KEY,build_id TEXT"
        " NOT NULL REFERENCES historical_jobs(build_id),partition_id TEXT NOT N"
        "ULL REFERENCES historical_partitions(id),page INTEGER NOT NULL,object_"
        "hash TEXT NOT NULL REFERENCES archive_objects(hash),payload BLOB NOT N"
        "ULL,UNIQUE(partition_id,page))",
        "CREATE TABLE historical_failed_acquisitions(id TEXT PRIMARY KEY,build_"
        "id TEXT NOT NULL REFERENCES historical_jobs(build_id),partition_id TEX"
        "T NOT NULL REFERENCES historical_partitions(id),object_hash TEXT NOT N"
        "ULL REFERENCES archive_objects(hash),payload BLOB NOT NULL)",
        "CREATE TABLE historical_observations(build_id TEXT NOT NULL REFERENCES"
        " historical_jobs(build_id),id TEXT NOT NULL,payload BLOB NOT NULL,PRIM"
        "ARY KEY(build_id,id))",
        "CREATE TABLE historical_facts(build_id TEXT NOT NULL REFERENCES histor"
        "ical_jobs(build_id),id TEXT NOT NULL,provider TEXT NOT NULL,resource T"
        "EXT NOT NULL,logical_id TEXT NOT NULL,revision INTEGER NOT NULL,availa"
        "ble INTEGER,kind TEXT NOT NULL,observation_id TEXT NOT NULL,payload BL"
        "OB NOT NULL,PRIMARY KEY(build_id,id),FOREIGN KEY(build_id,observation_"
        "id) REFERENCES historical_observations(build_id,id))",
        "CREATE INDEX historical_fact_query ON historical_facts(build_id,kind,a"
        "vailable,logical_id,revision)",
        "CREATE TABLE historical_lineage(build_id TEXT NOT NULL,fact_id TEXT NO"
        "T NULL,partition_id TEXT NOT NULL REFERENCES historical_partitions(id)"
        ",acquisition_id TEXT NOT NULL REFERENCES historical_acquisitions(id),p"
        "ayload BLOB NOT NULL,PRIMARY KEY(build_id,fact_id,partition_id,acquisi"
        "tion_id),FOREIGN KEY(build_id,fact_id) REFERENCES historical_facts(bui"
        "ld_id,id))",
        "CREATE INDEX historical_lineage_acquisition ON "
        "historical_lineage(build_id,acquisition_id,fact_id)",
        "CREATE TABLE historical_gaps(build_id TEXT NOT NULL REFERENCES histori"
        "cal_jobs(build_id),id TEXT NOT NULL,partition_id TEXT NOT NULL REFEREN"
        "CES historical_partitions(id),payload BLOB NOT NULL,PRIMARY KEY(build_"
        "id,id))",
        "CREATE TABLE dataset_manifests(build_id TEXT PRIMARY KEY REFERENCES hi"
        "storical_jobs(build_id),payload BLOB NOT NULL)",
        "CREATE TABLE historical_operations(id INTEGER PRIMARY KEY,build_id TEX"
        "T NOT NULL REFERENCES historical_jobs(build_id),payload BLOB NOT NULL)",
        "CREATE TRIGGER immutable_manifest_update BEFORE UPDATE ON dataset_mani"
        "fests BEGIN SELECT RAISE(ABORT,'immutable dataset manifest'); END",
        "CREATE TRIGGER immutable_manifest_delete BEFORE DELETE ON dataset_mani"
        "fests BEGIN SELECT RAISE(ABORT,'immutable dataset manifest'); END",
    )
    + tuple(
        f"CREATE TRIGGER frozen_{table}_{action.lower()} BEFORE {action} ON {table} "
        f"WHEN EXISTS(SELECT 1 FROM dataset_manifests WHERE build_id={row}.build_id) "
        + (
            "OR EXISTS(SELECT 1 FROM dataset_manifests WHERE build_id=NEW.build_id) "
            if action == "UPDATE"
            else ""
        )
        + "BEGIN SELECT RAISE(ABORT,'frozen dataset build'); END"
        for table in (
            "historical_jobs",
            "historical_partitions",
            "historical_acquisitions",
            "historical_failed_acquisitions",
            "historical_observations",
            "historical_facts",
            "historical_lineage",
            "historical_gaps",
        )
        for action, row in (("INSERT", "NEW"), ("UPDATE", "OLD"), ("DELETE", "OLD"))
    ),
    (
        "CREATE TABLE study_definitions(id TEXT PRIMARY KEY,event_id TEXT "
        "NOT NULL,instrument TEXT NOT NULL,anchor INTEGER NOT NULL,event_t"
        "ype TEXT NOT NULL,build_id TEXT NOT NULL REFERENCES dataset_manif"
        "ests(build_id),policy TEXT NOT NULL,payload BLOB NOT NULL)",
        "CREATE INDEX historical_event_study ON historical_facts"
        "(build_id,logical_id,kind,available)",
        "CREATE INDEX study_event ON study_definitions(event_id,instrument,anchor)",
        "CREATE INDEX study_class ON study_definitions(build_id,event_type,policy,anchor)",
        "CREATE INDEX study_anchor ON study_definitions(anchor,id)",
        "CREATE INDEX study_instrument ON study_definitions(instrument,anchor)",
        "CREATE TABLE study_results(id TEXT PRIMARY KEY REFERENCES study_d"
        "efinitions(id),available INTEGER NOT NULL,fingerprint TEXT NOT NU"
        "LL,payload BLOB NOT NULL)",
        "CREATE INDEX study_available ON study_results(available,id)",
        "CREATE TABLE study_runs(id TEXT PRIMARY KEY,build_id TEXT NOT NUL"
        "L REFERENCES dataset_manifests(build_id),payload BLOB NOT NULL)",
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable study artifact'); END"
        for table in ("study_definitions", "study_results", "study_runs")
        for action in ("UPDATE", "DELETE")
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
        if len(rows) > len(MIGRATIONS) or rows != [
            (i + 1, MIGRATION_VERSIONS[i]) for i in range(len(rows))
        ]:
            raise PersistenceError("unsupported database migration version")
        for index in range(len(rows), len(MIGRATIONS)):
            for statement in MIGRATIONS[index]:
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations VALUES (?,?)", (index + 1, MIGRATION_VERSIONS[index])
            )
        expected = {
            statement.split()[2].split("(")[0]: "".join(statement.split()).casefold()
            for migration in MIGRATIONS
            for statement in migration
            if statement.startswith(("CREATE TABLE ", "CREATE INDEX ", "CREATE TRIGGER "))
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
