# Compatibility and version policy

| Contract | Current version |
|---|---|
| Engine | 2.0.0 |
| Final snapshot / service API | 1.0 / 1.0 |
| SQLite store | 2.1, migration 12 |
| Existing public component schemas | 1.0, unchanged |
| Python | 3.11+ |
| Runtime dependencies | None |

The response version matrix lists each component independently. Native source/adapter/parser/model/prompt/policy/build versions remain in their retained owners and reference fingerprints. Unknown newer component schemas, final snapshot schemas, incompatible matrices, unknown migration versions, missing DDL and extra DDL fail closed. Static allowlisted typed JSON never imports a class from a stored type name. No pickle or executable deserialization is used.

Migration 12 adds final publications, scope membership, coverage declarations, retained expectation queries, snapshots and cutoff seals, with indices and immutable triggers. Migrations 1–11 retain exact DDL. Tests exercise empty stores and every prior migration prefix, ordered upgrade, restart, rollback, partial/future schema refusal and prior raw/event preservation. Existing schema-1.0 native contracts and all previous query APIs keep their meanings.

Final snapshot identity includes its explicit request, component selection, policy/build filters, limits, stable identity bindings, retained native references, coverage evidence and version matrix. Changing result-relevant settings creates a distinct snapshot. Operational metrics/correlation IDs and wall-clock generation timing are separate. Default valid time normalizes to the explicit known cutoff. There is no hidden latest clock.

The service API is locally versioned; remote authorization, tenancy, transport security and release distribution are deployment responsibilities. Future major API/schema changes require a separately versioned boundary rather than permissive decoding of unknown fields.
