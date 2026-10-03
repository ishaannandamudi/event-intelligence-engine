"""Ordered transactional SQLite schema, independent from quantitative schemas."""

import sqlite3

from eie.core.errors import PersistenceError

STORE_SCHEMA_VERSION = "1.9"
MIGRATION_VERSIONS = ("1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9")
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


MIGRATIONS += (
    (
        "CREATE TABLE te_contents(id TEXT PRIMARY KEY, content BLOB NOT NULL)",
        "CREATE TABLE te_documents(id TEXT PRIMARY KEY, dataset TEXT NOT NULL REFERENCES "
        "ce_datasets(id),"
        " raw TEXT NOT NULL REFERENCES observations(id), thread TEXT NOT NULL, "
        "content_hash TEXT NOT NULL,"
        " published INTEGER, observed INTEGER NOT NULL, payload BLOB NOT NULL)",
        "CREATE INDEX te_document_source ON te_documents(dataset,thread,observed,id)",
        "CREATE INDEX te_document_content ON te_documents(content_hash,id)",
        "CREATE INDEX te_document_published ON te_documents(published,id)",
        "CREATE TABLE te_maps(id TEXT PRIMARY KEY, original_hash TEXT NOT NULL, "
        "normalized_hash TEXT NOT NULL, payload BLOB NOT NULL)",
        "CREATE TABLE te_segments(id TEXT PRIMARY KEY, document TEXT NOT NULL REFERENCES "
        "te_documents(id),"
        " map TEXT NOT NULL REFERENCES te_maps(id), number INTEGER NOT NULL, start "
        "INTEGER NOT NULL,"
        " end INTEGER NOT NULL, payload BLOB NOT NULL, UNIQUE(document,map,number))",
        "CREATE INDEX te_segment_document ON te_segments(document,map,number)",
        "CREATE TABLE te_catalogs(id TEXT PRIMARY KEY, payload BLOB NOT NULL)",
        "CREATE TABLE te_templates(id TEXT PRIMARY KEY, template TEXT NOT NULL, version "
        "TEXT NOT NULL, payload BLOB NOT NULL)",
        "CREATE INDEX te_template_version ON te_templates(template,version,id)",
        "CREATE TABLE te_artifacts(id TEXT PRIMARY KEY, document TEXT NOT NULL "
        "REFERENCES te_documents(id),"
        " map TEXT NOT NULL REFERENCES te_maps(id), kind TEXT NOT NULL, segment TEXT "
        "REFERENCES te_segments(id),"
        " family TEXT, payload BLOB NOT NULL)",
        "CREATE INDEX te_artifact_document ON te_artifacts(document,kind,id)",
        "CREATE INDEX te_candidate_family ON te_artifacts(family,document,id)",
        "CREATE TABLE te_entity_members(artifact TEXT NOT NULL REFERENCES "
        "te_artifacts(id), entity TEXT NOT NULL,"
        " PRIMARY KEY(artifact,entity))",
        "CREATE INDEX te_entity_lookup ON te_entity_members(entity,artifact)",
        "CREATE TABLE te_runs(id TEXT PRIMARY KEY, document TEXT NOT NULL REFERENCES "
        "te_documents(id),"
        " map TEXT NOT NULL REFERENCES te_maps(id), catalog TEXT NOT NULL REFERENCES "
        "te_catalogs(id),"
        " known INTEGER NOT NULL, model TEXT, model_version TEXT, template TEXT "
        "REFERENCES te_templates(id),"
        " status TEXT NOT NULL, fingerprint TEXT NOT NULL, payload BLOB NOT NULL)",
        "CREATE INDEX te_run_knowledge ON te_runs(document,known,id)",
        "CREATE INDEX te_run_model ON te_runs(model,model_version,template,known,id)",
        "CREATE INDEX te_run_status ON te_runs(status,known,id)",
        "CREATE TABLE te_members(run TEXT NOT NULL REFERENCES te_runs(id), artifact TEXT "
        "NOT NULL REFERENCES te_artifacts(id),"
        " kind TEXT NOT NULL, PRIMARY KEY(run,artifact))",
        "CREATE TABLE te_run_segments(run TEXT NOT NULL REFERENCES te_runs(id), segment "
        "TEXT NOT NULL REFERENCES te_segments(id),"
        " PRIMARY KEY(run,segment))",
        "CREATE TABLE te_accepted(run TEXT NOT NULL REFERENCES te_runs(id), candidate "
        "TEXT NOT NULL REFERENCES te_artifacts(id),"
        " event TEXT NOT NULL, claim TEXT NOT NULL REFERENCES ce_claims(id), assertion "
        "TEXT NOT NULL REFERENCES ce_assertions(id),"
        " PRIMARY KEY(run,candidate,claim,assertion))",
        "CREATE INDEX te_accepted_claim ON te_accepted(claim,run,candidate)",
        "CREATE INDEX te_accepted_event ON te_accepted(event,run,candidate)",
        "CREATE TABLE te_retractions(run TEXT NOT NULL REFERENCES te_runs(id), assertion "
        "TEXT NOT NULL REFERENCES ce_assertions(id),"
        " PRIMARY KEY(run,assertion))",
        "CREATE TABLE te_span_evidence(run TEXT NOT NULL REFERENCES te_runs(id), span "
        "TEXT NOT NULL REFERENCES te_artifacts(id),"
        " evidence TEXT NOT NULL REFERENCES ce_references(id), PRIMARY KEY(run,span,evidence))",
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable text extraction artifact'); END"
        for table in (
            "te_contents",
            "te_documents",
            "te_maps",
            "te_segments",
            "te_catalogs",
            "te_templates",
            "te_artifacts",
            "te_entity_members",
            "te_runs",
            "te_members",
            "te_run_segments",
            "te_accepted",
            "te_retractions",
            "te_span_evidence",
        )
        for action in ("UPDATE", "DELETE")
    ),
)


MIGRATIONS += (
    (
        (
            "CREATE TABLE ei_policies(id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,payload "
            "BLOB NOT NULL)"
        ),
        (
            "CREATE TABLE ei_inputs(id TEXT PRIMARY KEY,dataset TEXT NOT NULL REFERENCES "
            "ce_datasets(id),policy TEXT NOT NULL REFERENCES ei_policies(id),known INTEGER "
            "NOT NULL,fingerprint TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        (
            "CREATE TABLE ei_results(id TEXT PRIMARY KEY,event TEXT NOT NULL,revision INTEGER"
            " NOT NULL,dataset TEXT NOT NULL REFERENCES ce_datasets(id),build TEXT,family "
            "TEXT NOT NULL,event_type TEXT NOT NULL,known INTEGER NOT NULL,available INTEGER "
            "NOT NULL,policy TEXT NOT NULL REFERENCES ei_policies(id),materiality TEXT NOT "
            "NULL,inputs TEXT NOT NULL REFERENCES ei_inputs(id),fingerprint TEXT NOT "
            "NULL,payload BLOB NOT NULL,FOREIGN KEY(event,revision) REFERENCES "
            "events(id,revision))"
        ),
        "CREATE INDEX ei_event_history ON ei_results(dataset,event,known,available,id)",
        "CREATE INDEX ei_type_history ON ei_results(dataset,family,event_type,known,id)",
        "CREATE INDEX ei_materiality_history ON ei_results(materiality,known,id)",
        "CREATE INDEX ei_build_policy ON ei_results(dataset,build,policy,known,id)",
        (
            "CREATE TABLE ei_entities(result TEXT NOT NULL REFERENCES ei_results(id),entity "
            "TEXT NOT NULL,PRIMARY KEY(result,entity))"
        ),
        "CREATE INDEX ei_entity_history ON ei_entities(entity,result)",
        (
            "CREATE TABLE ei_sources(result TEXT NOT NULL REFERENCES ei_results(id),family "
            "TEXT NOT NULL,PRIMARY KEY(result,family))"
        ),
        "CREATE INDEX ei_source_history ON ei_sources(family,result)",
        (
            "CREATE TABLE ei_measurements(result TEXT NOT NULL REFERENCES ei_results(id),"
            "measurement TEXT NOT NULL,claim TEXT NOT NULL REFERENCES ce_claims(id),"
            "PRIMARY KEY(result,measurement,claim))"
        ),
        "CREATE INDEX ei_measurement_history ON ei_measurements(measurement,result)",
        (
            "CREATE TABLE ei_novelty(result TEXT NOT NULL REFERENCES ei_results(id),dimension"
            " TEXT NOT NULL,subject TEXT NOT NULL,state TEXT NOT NULL,component TEXT NOT "
            "NULL,PRIMARY KEY(result,dimension,subject))"
        ),
        "CREATE INDEX ei_novelty_state ON ei_novelty(dimension,state,result)",
        (
            "CREATE TABLE ei_materiality(result TEXT NOT NULL REFERENCES ei_results(id),kind "
            "TEXT NOT NULL,subject TEXT NOT NULL,category TEXT NOT NULL,PRIMARY "
            "KEY(result,kind,subject))"
        ),
        "CREATE INDEX ei_materiality_component ON ei_materiality(kind,category,result)",
        (
            "CREATE TABLE ei_uncertainty(result TEXT NOT NULL REFERENCES ei_results(id),kind "
            "TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(result,kind))"
        ),
        "CREATE INDEX ei_uncertainty_state ON ei_uncertainty(kind,state,result)",
        (
            "CREATE TABLE ei_information(id TEXT PRIMARY KEY,kind TEXT NOT NULL,event TEXT "
            "NOT NULL,known INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX ei_information_history ON ei_information(kind,event,known,id)",
        (
            "CREATE TABLE ei_audits(id TEXT PRIMARY KEY,result TEXT NOT NULL REFERENCES "
            "ei_results(id),available INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload BLOB"
            " NOT NULL)"
        ),
        "CREATE INDEX ei_audit_history ON ei_audits(result,available,id)",
        (
            "CREATE TABLE ei_runs(id TEXT PRIMARY KEY,dataset TEXT NOT NULL REFERENCES "
            "ce_datasets(id),policy TEXT NOT NULL REFERENCES ei_policies(id),available "
            "INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        (
            "CREATE TABLE ei_run_members(run TEXT NOT NULL REFERENCES ei_runs(id),result TEXT"
            " NOT NULL REFERENCES ei_results(id),PRIMARY KEY(run,result))"
        ),
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable event intelligence artifact'); END"
        for table in (
            "ei_policies",
            "ei_inputs",
            "ei_results",
            "ei_entities",
            "ei_sources",
            "ei_measurements",
            "ei_novelty",
            "ei_materiality",
            "ei_uncertainty",
            "ei_information",
            "ei_audits",
            "ei_runs",
            "ei_run_members",
        )
        for action in ("UPDATE", "DELETE")
    ),
)


MIGRATIONS += (
    (
        (
            "CREATE TABLE analogue_policies(id TEXT PRIMARY KEY,fingerprint TEXT NOT NUL"
            "L,payload BLOB NOT NULL)"
        ),
        (
            "CREATE TABLE analogue_features(id TEXT PRIMARY KEY,event TEXT NOT NULL,revi"
            "sion INTEGER NOT NULL,dataset TEXT NOT NULL REFERENCES ce_datasets(id),buil"
            "d TEXT,family TEXT NOT NULL,event_type TEXT NOT NULL,event_available INTEGE"
            "R NOT NULL,known INTEGER NOT NULL,available INTEGER NOT NULL,policy TEXT NO"
            "T NULL REFERENCES analogue_policies(id),intelligence TEXT NOT NULL REFERENC"
            "ES ei_results(id),sector TEXT,industry TEXT,materiality TEXT NOT NULL,finge"
            "rprint TEXT NOT NULL,payload BLOB NOT NULL,FOREIGN KEY(event,revision) REFE"
            "RENCES events(id,revision))"
        ),
        (
            "CREATE INDEX analogue_type_block ON analogue_features(dataset,policy,event_"
            "type,event_available,id)"
        ),
        (
            "CREATE INDEX analogue_family_block ON analogue_features(dataset,policy,fami"
            "ly,event_available,id)"
        ),
        (
            "CREATE INDEX analogue_sector_block ON analogue_features(dataset,policy,sect"
            "or,event_available,id)"
        ),
        "CREATE INDEX analogue_industry_block ON analogue_features(industry,event_available,id)",
        (
            "CREATE INDEX analogue_materiality_block ON analogue_features(materiality,ev"
            "ent_available,id)"
        ),
        "CREATE INDEX analogue_known ON analogue_features(dataset,policy,known,id)",
        (
            "CREATE TABLE analogue_entities(row TEXT NOT NULL REFERENCES analogue_featur"
            "es(id),entity TEXT NOT NULL,PRIMARY KEY(row,entity))"
        ),
        "CREATE INDEX analogue_entity_block ON analogue_entities(entity,row)",
        (
            "CREATE TABLE analogue_measurements(row TEXT NOT NULL REFERENCES analogue_fe"
            "atures(id),measurement TEXT NOT NULL,PRIMARY KEY(row,measurement))"
        ),
        "CREATE INDEX analogue_measurement_block ON analogue_measurements(measurement,row)",
        (
            "CREATE TABLE analogue_contexts(row TEXT NOT NULL REFERENCES analogue_featur"
            "es(id),dimension TEXT NOT NULL,category TEXT NOT NULL,fingerprint TEXT NOT "
            "NULL,PRIMARY KEY(row,dimension))"
        ),
        "CREATE INDEX analogue_context_block ON analogue_contexts(dimension,category,row)",
        (
            "CREATE TABLE analogue_responses(id TEXT PRIMARY KEY,row TEXT NOT NULL REFER"
            "ENCES analogue_features(id),event TEXT NOT NULL,spec TEXT NOT NULL,metric T"
            "EXT NOT NULL,study TEXT NOT NULL REFERENCES study_results(id),impact TEXT R"
            "EFERENCES impact_results(id),completed INTEGER NOT NULL,available INTEGER N"
            "OT NULL,fingerprint TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX analogue_response_block ON analogue_responses(row,spec,available,id)",
        "CREATE INDEX analogue_metric_block ON analogue_responses(metric,available,id)",
        (
            "CREATE TABLE analogue_queries(id TEXT PRIMARY KEY,target_row TEXT NOT NULL "
            "REFERENCES analogue_features(id),known INTEGER NOT NULL,fingerprint TEXT NO"
            "T NULL,payload BLOB NOT NULL)"
        ),
        (
            "CREATE TABLE analogue_results(id TEXT PRIMARY KEY,query TEXT NOT NULL UNIQU"
            "E REFERENCES analogue_queries(id),target TEXT NOT NULL,revision INTEGER NOT"
            " NULL,dataset TEXT NOT NULL,known INTEGER NOT NULL,available INTEGER NOT NU"
            "LL,policy TEXT NOT NULL REFERENCES analogue_policies(id),fingerprint TEXT N"
            "OT NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX analogue_target_history ON analogue_results(target,known,available,id)",
        (
            "CREATE INDEX analogue_result_history ON analogue_results(dataset,policy,kno"
            "wn,available,id)"
        ),
        (
            "CREATE TABLE analogue_members(result TEXT NOT NULL REFERENCES analogue_resu"
            "lts(id),row TEXT NOT NULL REFERENCES analogue_features(id),response TEXT RE"
            "FERENCES analogue_responses(id),exclusion TEXT,fingerprint TEXT NOT NULL,PR"
            "IMARY KEY(result,row))"
        ),
        (
            "CREATE TABLE analogue_selected(result TEXT NOT NULL REFERENCES analogue_res"
            "ults(id),row TEXT NOT NULL REFERENCES analogue_features(id),distance REAL N"
            "OT NULL,fingerprint TEXT NOT NULL,PRIMARY KEY(result,row))"
        ),
        (
            "CREATE TABLE analogue_uncertainty(result TEXT NOT NULL REFERENCES analogue_"
            "results(id),kind TEXT NOT NULL,state TEXT NOT NULL,PRIMARY KEY(result,kind)"
            ")"
        ),
        (
            "CREATE TABLE analogue_reactions(id TEXT PRIMARY KEY,result TEXT NOT NULL RE"
            "FERENCES analogue_results(id),response TEXT REFERENCES analogue_responses(i"
            "d),available INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload BLOB NOT NU"
            "LL)"
        ),
        "CREATE INDEX analogue_reaction_history ON analogue_reactions(result,available,id)",
        (
            "CREATE TABLE analogue_runs(id TEXT PRIMARY KEY,dataset TEXT NOT NULL,availa"
            "ble INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload BLOB NOT NULL)"
        ),
        (
            "CREATE TABLE analogue_run_members(run TEXT NOT NULL REFERENCES analogue_run"
            "s(id),result TEXT NOT NULL REFERENCES analogue_results(id),PRIMARY KEY(run,"
            "result))"
        ),
        (
            "CREATE TABLE analogue_seals(dataset TEXT NOT NULL,policy TEXT NOT NULL REFE"
            "RENCES analogue_policies(id),known INTEGER NOT NULL,query TEXT NOT NULL REF"
            "ERENCES analogue_queries(id),PRIMARY KEY(dataset,policy,known,query))"
        ),
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable analogue artifact'); END"
        for table in (
            "analogue_policies",
            "analogue_features",
            "analogue_entities",
            "analogue_measurements",
            "analogue_contexts",
            "analogue_responses",
            "analogue_queries",
            "analogue_results",
            "analogue_members",
            "analogue_selected",
            "analogue_uncertainty",
            "analogue_reactions",
            "analogue_runs",
            "analogue_run_members",
            "analogue_seals",
        )
        for action in ("UPDATE", "DELETE")
    ),
)


# EIE-15 is additive; previous migration statements and payloads remain unchanged.
MIGRATIONS += (
    (
        (
            "CREATE TABLE interaction_events(id TEXT PRIMARY KEY,dataset TEXT NOT NUL"
            "L,event TEXT NOT NULL,revision INTEGER NOT NULL,revision_id TEXT NOT NUL"
            "L,family TEXT NOT NULL,type TEXT NOT NULL,scope TEXT NOT NULL,known INTE"
            "GER NOT NULL,source_time INTEGER,lo INTEGER,hi INTEGER,schedule_lo INTEG"
            "ER,schedule_hi INTEGER,thread TEXT NOT NULL,fingerprint TEXT NOT NULL,pa"
            "yload BLOB NOT NULL,UNIQUE(dataset,event,revision),FOREIGN KEY(event,rev"
            "ision) REFERENCES events(id,revision))"
        ),
        (
            "CREATE INDEX interaction_event_version ON interaction_events(dataset,eve"
            "nt,known,revision)"
        ),
        "CREATE INDEX interaction_event_time ON interaction_events(dataset,lo,hi,known)",
        (
            "CREATE INDEX interaction_schedule_time ON interaction_events(dataset,sch"
            "edule_lo,schedule_hi,known)"
        ),
        "CREATE INDEX interaction_source_time ON interaction_events(dataset,source_time,known)",
        "CREATE INDEX interaction_knowledge ON interaction_events(dataset,known)",
        "CREATE INDEX interaction_scope_time ON interaction_events(dataset,scope,lo,known)",
        "CREATE INDEX interaction_type_time ON interaction_events(dataset,type,lo,known)",
        "CREATE INDEX interaction_thread ON interaction_events(dataset,thread,known)",
        (
            "CREATE TABLE interaction_entities(row TEXT NOT NULL REFERENCES interacti"
            "on_events(id),entity TEXT NOT NULL,PRIMARY KEY(row,entity))"
        ),
        "CREATE INDEX interaction_entity ON interaction_entities(entity,row)",
        (
            "CREATE TABLE interaction_bundles(row TEXT NOT NULL REFERENCES interactio"
            "n_events(id),bundle TEXT NOT NULL,PRIMARY KEY(row,bundle))"
        ),
        "CREATE INDEX interaction_bundle ON interaction_bundles(bundle,row)",
        (
            "CREATE TABLE interaction_artifacts(id TEXT PRIMARY KEY,kind TEXT NOT NUL"
            "L,event TEXT,known INTEGER NOT NULL,fingerprint TEXT NOT NULL,payload BL"
            "OB NOT NULL)"
        ),
        "CREATE INDEX interaction_artifact_kind ON interaction_artifacts(kind,event,known)",
        (
            "CREATE TABLE interaction_contexts(id TEXT PRIMARY KEY,request TEXT NOT N"
            "ULL UNIQUE,dataset TEXT NOT NULL,target TEXT NOT NULL,known INTEGER NOT "
            "NULL,policy TEXT NOT NULL REFERENCES interaction_artifacts(id),manifest "
            "TEXT NOT NULL REFERENCES interaction_artifacts(id),fingerprint TEXT NOT "
            "NULL,payload BLOB NOT NULL)"
        ),
        "CREATE INDEX interaction_context_cutoff ON interaction_contexts(dataset,target,known)",
        (
            "CREATE TABLE interaction_members(owner TEXT NOT NULL REFERENCES interact"
            "ion_contexts(id),kind TEXT NOT NULL,member TEXT NOT NULL,PRIMARY KEY(own"
            "er,kind,member))"
        ),
        "CREATE INDEX interaction_member ON interaction_members(member,kind,owner)",
        (
            "CREATE TABLE interaction_pairs(collision TEXT NOT NULL REFERENCES intera"
            "ction_artifacts(id),left_row TEXT NOT NULL REFERENCES interaction_events"
            "(id),right_row TEXT NOT NULL REFERENCES interaction_events(id),type TEXT"
            " NOT NULL,PRIMARY KEY(collision,type))"
        ),
        "CREATE INDEX interaction_pair_type ON interaction_pairs(type,left_row,right_row)",
        "CREATE INDEX interaction_pair_right ON interaction_pairs(right_row,left_row)",
        (
            "CREATE TABLE interaction_chronology(id TEXT PRIMARY KEY REFERENCES inter"
            "action_artifacts(id),root TEXT NOT NULL REFERENCES interaction_events(id"
            "),later TEXT NOT NULL REFERENCES interaction_events(id),path TEXT NOT NU"
            "LL REFERENCES interaction_artifacts(id))"
        ),
        "CREATE INDEX interaction_chronology_root ON interaction_chronology(root,later)",
        "CREATE INDEX interaction_chronology_later ON interaction_chronology(later,root)",
        (
            "CREATE TABLE interaction_seals(dataset TEXT NOT NULL,known INTEGER NOT N"
            "ULL,PRIMARY KEY(dataset,known))"
        ),
    )
    + tuple(
        f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
        "BEGIN SELECT RAISE(ABORT,'immutable interaction artifact'); END"
        for table in (
            "interaction_events",
            "interaction_entities",
            "interaction_bundles",
            "interaction_artifacts",
            "interaction_contexts",
            "interaction_members",
            "interaction_pairs",
            "interaction_chronology",
            "interaction_seals",
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
