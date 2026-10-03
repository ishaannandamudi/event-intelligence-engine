# Optional model-assisted extraction

`StructuredExtractionModel` exposes a frozen model descriptor and
`extract(ExtractionRequest) -> bytes`. No vendor SDK, credentials, transport,
retry loop, browser or tool runtime is included. Automated tests and the runnable
example use `OfflineExtractionModel` with explicit deterministic JSON fixtures.
An externally supplied Python implementation is trusted adapter code; EIE supplies
no tool capabilities and does not claim to sandbox arbitrary adapter code.

## Request and output

A request has separate `trusted_template`, `model` and `source_data` fields.
The fixed trusted instructions explicitly label source as untrusted data and forbid
following commands, URLs, fake system messages, schema changes or tool instructions.
The complete trusted JSON schema is in `PromptTemplate.output_schema`. Source text
appears only in `UntrustedSourceData`, with source hash and segment offsets.

Output is bounded plain JSON with exactly `events`. Each proposal contains a safe
source-local key, controlled family/status, exact supporting segment span, resolved
entity IDs, typed numeric fields (including bounds/qualifiers), supported text fields,
optional temporal field and optional explicit reference period. All factual fields
need exact normalized offsets and SHA-256. `proposal_bytes()` is an offline fixture
encoder; `decode_proposals()` is the strict trust boundary. Unknown/missing fields,
wrong types, booleans as numbers, nonfinite values, duplicate JSON keys, excessive
arrays, unsafe keys and executable-looking objects fail safely.

Accepted output is still a proposal. The deterministic verifier supplies source
fact authority. It cannot use model confidence to establish existence, confirmation,
identity, numeric truth, time precision or a trading recommendation. Matching model
and deterministic candidates remain independently auditable and share deduplicated
source-local claim inputs. A disagreement remains a rejected/partial artifact.

## Processing provenance

Runs pin model ID/version/temperature, template ID/version/hash, full output schema,
parser/normalization/settings/schema versions, content/catalog hashes, request hash,
bounded raw output hash, all result IDs and predecessor. The output body is not
persisted; the strict parsed proposals remain auditable. Credential-shaped model IDs
and path-like identifiers are rejected. Raw source and request-body representations
are hidden by default. The original provider remains the claim source; the model
is processing provenance.

A new model/prompt/parser version creates new lineage and knowledge availability.
Old runs are immutable. Body-backed restart validation reconstructs the request and
source verification without contacting the model. A request descriptor with no
output hash can describe an unavailable model or a configured model not invoked
for unsupported language/source withdrawal; it does not certify a successful call.

Failures distinguish `MODEL_UNAVAILABLE`, `MODEL_TIMEOUT`, `SCHEMA_FAILURE`,
`INVALID_OUTPUT` and `VERIFICATION_FAILURE`. `NO_EVENT_EXTRACTED` is a processing
outcome, never proof that no real-world event occurred. Valid deterministic results
can commit as a partial run alongside a model failure.
