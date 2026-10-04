# Final snapshot service (EIE-17)

Engine 2.0.0; snapshot/API schema 1.0; SQLite 2.1. Existing public component schemas remain 1.0. The standard-library HTTP adapter is optional, serial, and restricted to IPv4 loopback. It starts only on an explicit `serve` command. Remote deployment, authentication, TLS and admission control belong to a separately operated gateway.

## Request

```json
{"catalog_id":"final-offline","scope":{"kind":"SYMBOL","identifiers":["EXM"]},"exchange":"NYSE","known_as_of_ns":1791676800000000000,"valid_at_ns":1788999000000000000,"components":["EVENTS","INTELLIGENCE"]}
```

`known_as_of_ns` is mandatory: integer UTC nanoseconds, nonnegative signed 64-bit range; booleans and strings are refused. Omitted `valid_at_ns` resolves explicitly to the cutoff and is stored in the normalized request. No deterministic API reads the wall clock. SYMBOL requires an exact uppercase symbol and exchange. COMPANY means stable issuer ID; SECURITY means stable security ID. INDUSTRY, SECTOR, INDEX, MARKET, MACRO_GLOBAL and SEC_FILER reuse existing scope taxonomy. MARKET covers the explicitly published catalog universe. Specific scopes also include published MARKET/MACRO_GLOBAL context; this does not turn related entities into direct event applicability.

Optional fields: sorted unique `components`, selected `required`, `policy_ids`, `build_ids`, `limits`, `schema_version`. Unknown fields, duplicate keys, invalid enums, nonfinite numbers and unsupported schemas fail closed. Arrays supplied through JSON are normalized to sorted selections. Policy/build filters apply to artifacts carrying those pins; unqualified artifacts remain visible and explicitly retain their provenance.

## Responses and routes

Every JSON envelope carries `engine_version`, `api_version`, `snapshot_schema`, the component version matrix and `value`. Canonical JSON uses deterministic field ordering. Snapshots contain immutable native references, bounded event summaries, component states, completeness, provenance and a normalized request. Their IDs/fingerprints are computed properties in Python and explicit fields in the wire snapshot view. The hash uses the typed semantic fields (excluding those two computed wire fields) with the `eie:service:EIESnapshot:v1` domain. Native owner IDs, fingerprints and publication IDs are explicit in the payload. Operational request IDs and timings are excluded.

| Method | Route | Input / output |
|---|---|---|
| GET | `/version` | Version matrix |
| GET | `/health` | Process alive |
| GET | `/ready` | Historical serving readiness; 503 when unavailable |
| GET | `/metrics` | Operational counters and accumulated durations |
| POST | `/snapshot` | One request / snapshot |
| POST | `/snapshots` | `{"requests":[...]}` / ordered per-item outcomes |
| POST | `/events`, `/upcoming` | Snapshot request / event component or scheduled summaries |
| POST | `/impact`, `/analogues`, `/relationships`, `/reliability` | Snapshot request / selected component |
| POST | `/expand` | `{"snapshot_id":"eies_…","owner_id":"…"}` / pinned owner detail |

Expansion requires membership in a retained snapshot. It cannot request arbitrary table rows, paths, URLs or documents. Raw document bodies are excluded. Owner detail can contain sourced labels/references; treat them as inert data.

Errors are categorical: INVALID_REQUEST, NOT_FOUND, AMBIGUOUS_IDENTITY, HISTORICAL_DATA_UNAVAILABLE, INCOMPLETE_DATA, BOUND_EXCEEDED, INTERNAL_VALIDATION_FAILURE, NOT_READY. External responses contain no exception messages or stack traces. HTTP errors use 400, 404, 500 or 503. HTTP framing overflow uses 413. A syntactically invalid whole JSON body fails the request. A valid batch envelope returns per-item failures for malformed items or unresolved symbols. Aggregate response overflow rolls back newly created batch snapshots.

## States and coverage

AVAILABLE says retained artifacts exist; it does not assert complete coverage. UNAVAILABLE differs from an explicitly verified empty universe. INCOMPLETE, STALE, CONFLICTED and NOT_APPLICABLE remain categorical. Completeness is COMPLETE, KNOWN_GAPS, UNKNOWN or NOT_APPLICABLE, independently per component. Only an explicit trusted coverage declaration can assert COMPLETE or known-empty. An empty extraction with failure remains incomplete. Required unavailable/incomplete components fail; conflicted/stale components remain available with qualifications.

## Default bounds

19 selected components; 256 references per component; 10,000 identity rows; 1 MB snapshot; 5 MB expansion; 500 batch items; 20 MB batch response; 1 MB request. Request limits may reduce server ceilings. Result truncation is never silent: reference overflow fails BOUND_EXCEEDED. Graph depth, analogue count, study windows and interaction paths retain their original owner policy bounds. This boundary queries one instant; interval scans are not exposed. HTTP runs one active worker, backlog 16, connection read/write timeout 5 seconds. The timeout is not a calculation deadline; CPU work is bounded by request/artifact caps. Port/body/batch bounds require deployment-specific sizing.
