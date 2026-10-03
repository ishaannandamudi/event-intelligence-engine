# EIE-12 completion report

Date: 2026-10-03. Repository: `event-intelligence-engine`.
Resumed the current working tree from committed EIE-11 (`89df7d2`).
Changes remain uncommitted. No push or EIE-13 implementation.

## A–AN report

| Item | Result |
| --- | --- |
| A. Architecture | Document → mapped normalization → source mentions and deterministic proposals → optional model proposals → mandatory source verification → atomic EIE-11 claims and extraction manifest. |
| B. Package structure | `eie.nlp` contains frozen contracts, normalization, segmentation, numeric/time/entity extraction, classification, prompts, strict output decoding, verification, linkage, engine, repository, queries/audit and validation. `eie.providers.text` supplies trusted offline document envelopes. Thirty new source files; no transport layer. |
| C. Text-extraction schema | `TEXT_EXTRACTION_SCHEMA_VERSION = "1.0"`. Typed, frozen dataclasses, closed enums, strict plain model JSON and an additive persistence codec allowlist. |
| D. Document model | Source observation, content hash, source item/version/family, dataset/resource, document type/format/language, acquisition lineage, retention, timezone/currency, source action and predecessor. Source provenance remains separate from model provenance. |
| E. Normalization | Strict UTF-8; explicit controls/size policy; CRLF/CR to LF; Unicode NFC clusters; inert restricted HTML conversion. Hash-pinned maps retain exact original character ranges, including composed characters and entities. |
| F. Segmentation | Dense headline/paragraph segments preserve offsets and hashes. Explicit management clauses can yield multiple candidates while retaining the complete parent segment as supporting and security context. |
| G. Numeric extraction | Reuses EIE-5 quantities/units. Currency, scale, percentage, basis points, per-share values, signs/parentheses and compatible ranges are supported. Metric qualifiers remain semantic identity. Integer precision is retained; unsupported fractions are omitted. Ambiguous scale, incompatible ranges, competing values and exclusive bounds cannot become unsupported point/inclusive facts. |
| H. Temporal extraction | Exact offset timestamp, date-only, date-plus-session and unknown precision remain distinct. Qualified publication timezone supports today/tomorrow. Event/effective time requires a supported cue. Reference periods require an explicit fiscal year/quarter and end date. No precision or quarter-end fabrication. |
| I. Entity mentions/resolution | Known SEC issuer/catalog and historical listing snapshots support exact CIK/ticker matching. Name candidates remain unresolved; person/role mentions remain lightweight literals. Future identity metadata cannot silently resolve an earlier extraction. |
| J. Taxonomy/classification | Twenty controlled extraction families map into existing event taxonomy, including earnings/guidance, management, corporate actions, legal/regulatory, cybersecurity, product/operations and other explicit announcements. Classification uses conservative source cues. |
| K. Candidate model | Immutable source-local proposals retain family, epistemic status, exact supporting segment, issuer IDs, typed fields and field spans, plus method/quality. Rejected and unresolved candidates remain auditable. |
| L. Extraction confidence | Categorical method/quality and four verification states. No opaque aggregate probability, calibrated confidence claim, source-reliability upgrade or materiality score. |
| M. Model protocol | Provider-neutral descriptor plus `extract(ExtractionRequest) -> bytes`. Deterministic fake model fixtures cover normal/error/adversarial output. Caller-supplied Python adapters are trusted code; arbitrary adapter execution is not sandboxed by this interface. |
| N. Prompt/model fingerprints | Runs pin model ID/version/temperature, trusted template/schema hash, parser/normalization settings, catalog/content hashes, request/output hashes, exact result IDs, availability and predecessor. Output bodies and request bodies are not persisted. |
| O. Prompt-injection isolation | Trusted fixed instructions/schema and `UntrustedSourceData` are separate typed fields. No source-supplied tools, runtime configuration, paths, imports or executable objects. Instruction detection additionally restricts factual acceptance; mandatory source verification remains the authority boundary. |
| P. Evidence spans | Document/map/segment IDs, normalized/original offsets and both fragment hashes. EIE-11 references use original CHARACTER locations. Multiple spans can support a claim; document bodies are not duplicated into claim rows. |
| Q. Verification | `VERIFIED`, `PARTIALLY_VERIFIED`, `UNVERIFIED`, `REJECTED` with fixed reasons. Values, units, bounds, metric/qualifiers, period, entities, temporal precision, literal fields, full context and epistemic status must agree with source evidence. Only verified candidates enter canonical state. |
| R. Negation/rumor/denial | Denied, negated, rumored, alleged, planned, expected and conditional candidates remain unaccepted/partial. Reports remain reported. Guidance issuance does not imply realized guidance; an acquisition announcement does not establish a closed deal. Failure/no-event extraction does not prove event absence. |
| S. Multi-event documents | Shared document provenance does not merge distinct events. Management grouping uses grounded issuer, person, role, action and effective identity. Different people/roles/actions/times stay separate. Repeated complete identities may share a source-local event; undated repetitions remain conservative. Model keys cannot select grouping or mix fields between clauses. EIE-11 false-merge rules are unchanged. |
| T. EIE-11 claim linkage | Accepted occurrence/time, earnings/guidance numeric/range and compatible corporate action facts pass through the existing `EvidenceEngine`. Exact measurement proposition identity remains separate from retained source event envelopes. EIE-11 owns cross-source conflicts, corroboration, source independence and event aliases. |
| U. Source/model lineage | Additive `ExtractionAcquisition` preserves original acquisition clocks and adds processing availability/fingerprint. Historical text pins completed EIE-7 build/fact/archive acquisition. Knowledge cannot precede actual acquisition and extraction. |
| V. Reprocessing | Same exact inputs are idempotent. New source/model/prompt/parser versions or processing availability produce immutable runs and explicit EIE-11 updates/corrections. Source withdrawals append retractions with withdrawal evidence. Earlier audits and historical cutoffs remain unchanged. |
| W. Persistence/migrations | Store `1.6`, migration seven: fourteen immutable text/content/index/link tables with foreign keys and triggers. All six prior migration statement tuples are byte-for-byte unchanged. Accepted claims, checkpoint, extraction artifacts and links commit together or roll back. |
| X. Query/audit APIs | Explicit cutoff APIs expose documents, runs, family/entity candidates, mentions, failures, accepted assertions and event audits. Audit traversal connects candidate → verification → claim/assertion → original evidence → segment/map → document/raw observation. Bounds fail instead of truncating silently. |
| Y. Validation | Recalculates typed identities/indexes, full memberships, receipts, accepted facts and processing lineage. Retained bodies allow normalization/mention/span/source-verification/request recalculation. Metadata-only retention reports `VALID_WITH_WARNINGS`, not invented body verification. |
| Z. Performance/storage | Explicit document/token/segment/mention/candidate/span/output/claim/query bounds. Original licensed bytes are content-addressed once; metadata-only providers cannot borrow another provider's body. The stored profile uses 25 documents and 20 unique bodies, not 10,000 persisted documents. Detailed timings below. |
| AA. Security | Fake system/tool/shell/URL/credential/schema instructions stay inert. Strict output rejects unknown/missing fields, wrong primitives, duplicates, unsafe keys and excessive arrays. Configured candidate limits cannot relax the fixed model schema ceiling. Socket-blocked verification/profile make no network calls. No vendor SDK, executable deserialization or source-triggered execution. |
| AB. Documentation | Added text ingestion, structured extraction, model assistance, NLP security and unscheduled-event guides. Updated README, master status and eight architecture/contract/policy/replay/evidence/dedup/persistence guides. Added this report and runnable offline example/profile. |
| AC. Tests added | 638 EIE-12 cases across nine test modules plus shared fixtures. Coverage includes contract/type matrices, Unicode/HTML maps, source spans, numeric/temporal/entity safeguards, all families, adversarial model output, management identity, EIE-7 lineage, EIE-11 linkage/conflicts, retention, atomic rollback, concurrency, corruption, migration, reprocessing and restart. Existing test edits only update current engine/store version expectations. |
| AD. Full pytest | **2,195 passed in 223.30 seconds** in the full socket-blocked branch-enabled run: all 1,557 baseline cases plus 638 EIE-12 cases. **Zero network connection attempts.** The final focused EIE-12 run also passed all 638 cases in 34.12 seconds; management/numeric focused run: 39 passed; schema/security focused run: 42 passed. |
| AE. Coverage | **96.09063% combined statement/branch coverage**, exceeding the configured 95% gate. Statements: 17,174 / 17,651 (97.29760%); branches: 5,685 / 6,138 (92.61975%). New text/provider packages: 93.43041% combined; strict output/security module: 100%; verification: 94.91525%. Collection metadata confirms branch coverage enabled. |
| AF. Ruff | Lint passes; format check passes for all 373 checked files, including README code blocks and this report. |
| AG. mypy | Strict checking passes all 236 source files. |
| AH. Diff checks/review | Tracked changes and new implementation/test/doc/profile files reviewed; whitespace check passes. Prior migration preservation and prior schema/version changes reviewed explicitly. Final status retains only EIE-12 changes on `main`; HEAD remains `89df7d2`. README/documentation local links resolve. |
| AI. Scans | Full tracked/untracked credential/private-key scan: no secrets. No new unfinished markers or trailing whitespace. Two pre-existing documentation references name review gates (`EIE-5-completion.md:51`, master specification `:1618`); neither is unfinished implementation. |
| AJ. Benchmark/profile | Completed socket-blocked 10,000-document profile with 50 fake-model calls, 25 persisted documents, audit queries and body-backed restart validation. Zero connection attempts and byte-identical restart audits. Results saved in `benchmarks/EIE-12-profile.json`; no CI timing threshold. |
| AK. Versions | Engine/package `1.3.0`; text extraction `1.0`; SQLite store `1.6`. All prior public schemas remain `1.0`. |
| AL. Dependencies | No runtime or development dependencies added. Standard library implementation; existing Python 3.11.16/tooling reused read-only without changing a sibling repository. |
| AM. Remaining limitations | English and restricted deterministic grammar; names alone unresolved; arbitrary tables and narrative event identity unsupported. Most repeated same-family contexts remain partial outside explicit management clauses. Exclusive bounds or unsupported precision remain artifacts/omissions. No vendor model transport or calibrated probability. Metadata-only replay cannot recheck missing bodies. Persistence/audit/restart costs are substantial in this correctness-focused implementation; profile timings are observational, not production throughput guarantees. |
| AN. Exact proposed EIE-13 scope | Novelty + Materiality + Uncertainty Decomposition: document, factual, numeric, guidance, entity and cross-source novelty; static importance separated from event-specific materiality; empirical impact kept separate; uncertainty decomposed into source, entity, classification, numeric, event identity, materiality, analogue and impact components. No opaque single confidence score or trading recommendations. This scope is proposed only; no EIE-13 work implemented. |

## Offline profile

The workload uses small synthetic three-event press releases with numeric variation.
The normalization/mention and deterministic-proposal measurements are separate.
The full extraction/verification measurement includes preprocessing again; these
timings are not disjoint components of the overall elapsed preprocessing time.

| Measurement | Observed result |
| --- | ---: |
| Documents preprocessed / persisted | 10,000 / 25 |
| Characters | 2,250,000 |
| Candidates / spans | 30,150 / 160,000 |
| Fake model calls / network attempts | 50 / 0 |
| Overall preprocessing | 62.96 seconds |
| Preprocessing throughput | 158.84 documents/s; 35,738.49 characters/s |
| Normalization and mentions | 19.66 seconds |
| Deterministic proposals | 3.42 seconds |
| Full extraction and verification | 39.31 seconds |
| Persistence of 25 documents | 75.67 seconds |
| Audit queries for 25 runs | 60.69 seconds |
| Restart audits and complete validation | 162.00 seconds |
| SQLite storage | 16,629,760 bytes |
| Stored documents / bodies / artifacts / runs | 25 / 20 / 806 / 25 |
| EIE-11 claims / assertions / references | 152 / 200 / 350 |
| Accepted candidate/assertion link rows | 208 |
| Restart audit equality | Byte-identical |

The database includes existing EIE-11 derived evidence/resolution history in addition
to text artifacts. These timings expose persistence/audit costs on this development
host and are not extrapolated to a larger persisted corpus.

## Stop condition

EIE-12 only. Novelty/materiality, historical analogues, collision modeling, final
service/API, MCE/AME integration and trading remain deferred. No commit or push.
