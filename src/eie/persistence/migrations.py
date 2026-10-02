"""Ordered transactional SQLite schema, independent from quantitative schemas."""

import sqlite3

from eie.core.errors import PersistenceError

STORE_SCHEMA_VERSION = "1.5"
MIGRATION_VERSIONS = ("1.0", "1.1", "1.2", "1.3", "1.4", "1.5")
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
    (
        "CREATE TABLE impact_models(id TEXT PRIMARY KEY,study_id TEXT NOT NULL REFERENCES "
        "study_results(id),build_id TEXT NOT NULL REFERENCES dataset_manifests(build_id),"
        "event_id TEXT NOT NULL,instrument TEXT NOT NULL,anchor INTEGER NOT NULL,"
        "event_type TEXT NOT NULL,model_type TEXT NOT NULL,inputs TEXT NOT NULL,"
        "spec BLOB NOT NULL,fit BLOB NOT NULL)",
        "CREATE INDEX impact_event ON impact_models(event_id,instrument,anchor)",
        "CREATE INDEX impact_class ON impact_models(build_id,event_type,model_type,anchor)",
        "CREATE INDEX impact_instrument ON impact_models(instrument,anchor)",
        "CREATE TABLE impact_results(id TEXT PRIMARY KEY,model_id TEXT NOT NULL UNIQUE "
        "REFERENCES impact_models(id),available INTEGER NOT NULL,fingerprint TEXT NOT NULL,"
        "payload BLOB NOT NULL)",
        "CREATE INDEX impact_available ON impact_results(available,id)",
        "CREATE TABLE impact_sets(id TEXT PRIMARY KEY,study_id TEXT NOT NULL REFERENCES "
        "study_results(id),payload BLOB NOT NULL)",
        "CREATE INDEX impact_set_study ON impact_sets(study_id,id)",
        "CREATE TABLE impact_runs(id TEXT PRIMARY KEY,build_id TEXT NOT NULL REFERENCES "
        "dataset_manifests(build_id),payload BLOB NOT NULL)",
        "CREATE INDEX impact_run_build ON impact_runs(build_id,id)",
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable impact artifact'); END"
        for table in ("impact_models", "impact_results", "impact_sets", "impact_runs")
        for action in ("UPDATE", "DELETE")
    ),
    (
        "CREATE TABLE graph_entities(id TEXT PRIMARY KEY,kind TEXT NOT NULL)",
        (
            "CREATE TABLE graph_nodes(id TEXT PRIMARY KEY,node TEXT NOT NULL REFERENCES "
            "graph_entities(id),known INTEGER NOT NULL,dataset TEXT NOT NULL,payload BLOB NOT "
            "NULL,UNIQUE(node,known))"
        ),
        "CREATE INDEX graph_node_time ON graph_nodes(node,known)",
        "CREATE INDEX graph_node_known ON graph_nodes(known)",
        (
            "CREATE TABLE graph_identifiers(revision TEXT NOT NULL REFERENCES graph_nodes(id),"
            "authority TEXT NOT NULL,value TEXT NOT NULL,node TEXT NOT NULL REFERENCES "
            "graph_entities(id),PRIMARY KEY(revision,authority,value))"
        ),
        "CREATE INDEX graph_exact_identifier ON graph_identifiers(authority,value,node)",
        "CREATE TABLE graph_listing_index(revision TEXT PRIMARY KEY REFERENCES graph_nodes(id),"
        "node TEXT NOT NULL REFERENCES graph_entities(id),ticker TEXT NOT NULL,exchange TEXT "
        "NOT NULL,issuer TEXT NOT NULL REFERENCES graph_entities(id),valid_from INTEGER NOT "
        "NULL,valid_to INTEGER)",
        "CREATE INDEX graph_symbol_time ON graph_listing_index(ticker,exchange,valid_from,"
        "valid_to)",
        (
            "CREATE TABLE graph_edges(id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_node TEXT "
            "NOT NULL REFERENCES graph_entities(id),target_node TEXT NOT NULL REFERENCES "
            "graph_entities(id))"
        ),
        "CREATE INDEX graph_edge_source ON graph_edges(source_node,kind)",
        "CREATE INDEX graph_edge_target ON graph_edges(target_node,kind)",
        (
            "CREATE TABLE graph_claims(id TEXT PRIMARY KEY,claim_id TEXT NOT NULL,number "
            "INTEGER NOT NULL,previous TEXT REFERENCES graph_claims(id),edge_id TEXT "
            "REFERENCES graph_edges(id),provider TEXT NOT NULL,resource TEXT NOT NULL,known "
            "INTEGER NOT NULL,valid_from INTEGER,valid_to INTEGER,semantics TEXT NOT NULL,kind "
            "TEXT NOT NULL,source_node TEXT REFERENCES graph_entities(id),target_node TEXT "
            "REFERENCES graph_entities(id),exposure_type TEXT,observation TEXT NOT NULL "
            "REFERENCES observations(id),payload BLOB NOT NULL,UNIQUE(claim_id,number))"
        ),
        "CREATE INDEX graph_claim_source ON graph_claims(source_node,known)",
        "CREATE INDEX graph_claim_target ON graph_claims(target_node,known)",
        "CREATE INDEX graph_claim_edge ON graph_claims(edge_id,known)",
        "CREATE INDEX graph_claim_history ON graph_claims(claim_id,known,number)",
        "CREATE INDEX graph_claim_known ON graph_claims(known)",
        "CREATE INDEX graph_claim_provider ON graph_claims(provider,resource,known)",
        "CREATE INDEX graph_claim_type_time ON graph_claims(kind,valid_from,valid_to,known)",
        "CREATE INDEX graph_claim_exposure ON graph_claims(exposure_type,known)",
        (
            "CREATE TABLE graph_edge_revisions(id TEXT PRIMARY KEY,edge_id TEXT NOT NULL "
            "REFERENCES graph_edges(id),known INTEGER NOT NULL,active INTEGER NOT NULL,"
            "exposure_type TEXT,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX graph_canonical_history ON graph_edge_revisions(edge_id,known)",
        (
            "CREATE TABLE graph_batches(id TEXT PRIMARY KEY,provider TEXT NOT NULL,resource "
            "TEXT NOT NULL,known INTEGER NOT NULL,snapshot TEXT NOT NULL,observation TEXT NOT "
            "NULL REFERENCES observations(id),generation INTEGER NOT NULL,fingerprint TEXT NOT "
            "NULL,payload BLOB NOT NULL,UNIQUE(provider,resource,generation))"
        ),
        "CREATE INDEX graph_batch_time ON graph_batches(provider,resource,known)",
        "CREATE INDEX graph_batch_known ON graph_batches(known)",
        "CREATE INDEX graph_source_snapshot ON graph_batches(snapshot)",
        (
            "CREATE TABLE graph_snapshots(id TEXT PRIMARY KEY,known INTEGER NOT NULL,valid "
            "INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX graph_snapshot_time ON graph_snapshots(known,valid)",
        (
            "CREATE TABLE graph_snapshot_edges(snapshot TEXT NOT NULL REFERENCES "
            "graph_snapshots(id),revision TEXT NOT NULL REFERENCES graph_edge_revisions(id),"
            "PRIMARY KEY(snapshot,revision))"
        ),
        (
            "CREATE TABLE graph_snapshot_nodes(snapshot TEXT NOT NULL REFERENCES "
            "graph_snapshots(id),revision TEXT NOT NULL REFERENCES graph_nodes(id),PRIMARY "
            "KEY(snapshot,revision))"
        ),
        (
            "CREATE TABLE graph_propagation(id TEXT PRIMARY KEY,snapshot TEXT REFERENCES "
            "graph_snapshots(id),event_revision TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX graph_propagation_event ON graph_propagation(event_revision)",
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable graph artifact'); END"
        for table in (
            "graph_entities",
            "graph_nodes",
            "graph_identifiers",
            "graph_listing_index",
            "graph_edges",
            "graph_claims",
            "graph_edge_revisions",
            "graph_batches",
            "graph_snapshots",
            "graph_snapshot_edges",
            "graph_snapshot_nodes",
            "graph_propagation",
        )
        for action in ("UPDATE", "DELETE")
    ),
)


MIGRATIONS += (
    (
        "CREATE TRIGGER protected_ce_raw_update BEFORE UPDATE ON observations "
        "WHEN EXISTS(SELECT 1 FROM ce_acquisitions WHERE raw=OLD.id) "
        "BEGIN SELECT RAISE(ABORT,'immutable claim evidence source'); END",
        (
            "CREATE TABLE ce_datasets(id TEXT PRIMARY KEY,build TEXT,fingerprint TE"
            "XT NOT NULL,payload BLOB NOT NULL)"
        ),
        (
            "CREATE TABLE ce_acquisitions(id TEXT PRIMARY KEY,raw TEXT NOT NULL REF"
            "ERENCES observations(id),dataset TEXT NOT NULL REFERENCES ce_datasets("
            "id),known INTEGER NOT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX ce_acquisition_raw ON ce_acquisitions(raw,dataset,known)",
        (
            "CREATE TABLE ce_claims(id TEXT PRIMARY KEY,subject TEXT NOT NULL,predi"
            "cate TEXT NOT NULL,kind TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX ce_claim_subject ON ce_claims(subject,predicate,kind)",
        (
            "CREATE TABLE ce_references(id TEXT PRIMARY KEY,raw TEXT NOT NULL REFER"
            "ENCES observations(id),content_hash TEXT NOT NULL,parser TEXT NOT NULL"
            ",payload BLOB NOT NULL)"
        ),
        "CREATE INDEX ce_reference_raw ON ce_references(raw,parser)",
        (
            "CREATE TABLE ce_assertions(id TEXT PRIMARY KEY,thread TEXT NOT NULL,cl"
            "aim TEXT NOT NULL REFERENCES ce_claims(id),dataset TEXT NOT NULL REFER"
            "ENCES ce_datasets(id),provider TEXT NOT NULL,family TEXT NOT NULL,reso"
            "urce TEXT NOT NULL,raw TEXT NOT NULL REFERENCES observations(id),linea"
            "ge TEXT NOT NULL REFERENCES ce_acquisitions(id),number INTEGER NOT NUL"
            "L,known INTEGER NOT NULL,valid_from INTEGER,valid_to INTEGER,event TEX"
            "T,payload BLOB NOT NULL,UNIQUE(thread,number))"
        ),
        "CREATE INDEX ce_assertion_claim ON ce_assertions(dataset,claim,known)",
        "CREATE INDEX ce_assertion_provider ON ce_assertions(dataset,provider,resource,known)",
        "CREATE INDEX ce_assertion_raw ON ce_assertions(raw,known)",
        "CREATE INDEX ce_assertion_valid ON ce_assertions(dataset,valid_from,valid_to,known)",
        "CREATE INDEX ce_assertion_event ON ce_assertions(dataset,event,known)",
        (
            "CREATE TABLE ce_assertion_evidence(assertion TEXT NOT NULL REFERENCES "
            "ce_assertions(id),evidence TEXT NOT NULL REFERENCES ce_references(id),"
            "PRIMARY KEY(assertion,evidence))"
        ),
        "CREATE INDEX ce_evidence_assertions ON ce_assertion_evidence(evidence,assertion)",
        (
            "CREATE TABLE ce_relations(id TEXT PRIMARY KEY,source TEXT NOT NULL REF"
            "ERENCES ce_assertions(id),target TEXT NOT NULL REFERENCES ce_assertion"
            "s(id),dataset TEXT NOT NULL REFERENCES ce_datasets(id),known INTEGER N"
            "OT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX ce_relation_source ON ce_relations(dataset,source,known)",
        "CREATE INDEX ce_relation_target ON ce_relations(dataset,target,known)",
        (
            "CREATE TABLE ce_descriptors(id TEXT PRIMARY KEY,event TEXT NOT NULL,ev"
            "ent_revision INTEGER NOT NULL,dataset TEXT NOT NULL REFERENCES ce_data"
            "sets(id),thread TEXT NOT NULL,raw TEXT NOT NULL REFERENCES observation"
            "s(id),number INTEGER NOT NULL,known INTEGER NOT NULL,payload BLOB NOT "
            "NULL,UNIQUE(thread,number),FOREIGN KEY(event,event_revision) REFERENCE"
            "S events(id,revision))"
        ),
        "CREATE INDEX ce_descriptor_event ON ce_descriptors(dataset,event,known)",
        "CREATE INDEX ce_descriptor_known ON ce_descriptors(dataset,known)",
        (
            "CREATE TABLE ce_blocks(descriptor TEXT NOT NULL REFERENCES ce_descript"
            "ors(id),block TEXT NOT NULL,PRIMARY KEY(descriptor,block))"
        ),
        "CREATE INDEX ce_block_candidates ON ce_blocks(block,descriptor)",
        (
            "CREATE TABLE ce_claim_resolutions(id TEXT PRIMARY KEY,claim TEXT NOT N"
            "ULL REFERENCES ce_claims(id),dataset TEXT NOT NULL REFERENCES ce_datas"
            "ets(id),known INTEGER NOT NULL,valid INTEGER,state TEXT NOT NULL,polic"
            "y TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX ce_resolution_history ON ce_claim_resolutions(dataset,claim,known,valid)",
        (
            "CREATE TABLE ce_resolution_inputs(resolution TEXT NOT NULL REFERENCES "
            "ce_claim_resolutions(id),assertion TEXT NOT NULL REFERENCES ce_asserti"
            "ons(id),PRIMARY KEY(resolution,assertion))"
        ),
        (
            "CREATE TABLE ce_resolution_relations(resolution TEXT NOT NULL REFERENC"
            "ES ce_claim_resolutions(id),relation TEXT NOT NULL REFERENCES ce_relat"
            "ions(id),PRIMARY KEY(resolution,relation))"
        ),
        (
            "CREATE TABLE ce_candidates(id TEXT PRIMARY KEY,left_id TEXT NOT NULL R"
            "EFERENCES ce_descriptors(id),right_id TEXT NOT NULL REFERENCES ce_desc"
            "riptors(id),decision TEXT NOT NULL,policy TEXT NOT NULL,payload BLOB N"
            "OT NULL)"
        ),
        "CREATE INDEX ce_candidate_pair ON ce_candidates(left_id,right_id,policy)",
        (
            "CREATE TABLE ce_event_runs(id TEXT PRIMARY KEY,dataset TEXT NOT NULL R"
            "EFERENCES ce_datasets(id),known INTEGER NOT NULL,policy TEXT NOT NULL,"
            "payload BLOB NOT NULL)"
        ),
        "CREATE INDEX ce_run_history ON ce_event_runs(dataset,known)",
        (
            "CREATE TABLE ce_run_inputs(run TEXT NOT NULL REFERENCES ce_event_runs("
            "id),descriptor TEXT NOT NULL REFERENCES ce_descriptors(id),PRIMARY KEY"
            "(run,descriptor))"
        ),
        (
            "CREATE TABLE ce_run_candidates(run TEXT NOT NULL REFERENCES ce_event_r"
            "uns(id),candidate TEXT NOT NULL REFERENCES ce_candidates(id),PRIMARY K"
            "EY(run,candidate))"
        ),
        (
            "CREATE TABLE ce_group_members(run TEXT NOT NULL REFERENCES ce_event_ru"
            "ns(id),group_id TEXT NOT NULL,descriptor TEXT NOT NULL REFERENCES ce_d"
            "escriptors(id),PRIMARY KEY(run,group_id,descriptor))"
        ),
        "CREATE INDEX ce_group_history ON ce_group_members(group_id,run)",
        (
            "CREATE TABLE ce_receipts(id TEXT PRIMARY KEY,dataset TEXT NOT NULL REF"
            "ERENCES ce_datasets(id),provider TEXT NOT NULL,resource TEXT NOT NULL,"
            "known INTEGER NOT NULL,generation INTEGER NOT NULL,raw TEXT NOT NULL R"
            "EFERENCES observations(id),lineage TEXT NOT NULL REFERENCES ce_acquisi"
            "tions(id),payload BLOB NOT NULL,UNIQUE(dataset,provider,resource,gener"
            "ation))"
        ),
        "CREATE INDEX ce_receipt_checkpoint ON ce_receipts(dataset,provider,resource,generation)",
        (
            "CREATE TABLE ce_seals(dataset TEXT NOT NULL REFERENCES ce_datasets(id)"
            ",known INTEGER NOT NULL,PRIMARY KEY(dataset,known))"
        ),
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable claim evidence artifact'); END"
        for table in (
            "ce_datasets",
            "ce_acquisitions",
            "ce_claims",
            "ce_references",
            "ce_assertions",
            "ce_assertion_evidence",
            "ce_relations",
            "ce_descriptors",
            "ce_blocks",
            "ce_claim_resolutions",
            "ce_resolution_inputs",
            "ce_resolution_relations",
            "ce_candidates",
            "ce_event_runs",
            "ce_run_inputs",
            "ce_run_candidates",
            "ce_group_members",
            "ce_receipts",
            "ce_seals",
        )
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
