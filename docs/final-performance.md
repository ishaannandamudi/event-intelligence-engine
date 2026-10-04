# Final snapshot performance (EIE-17)

These are single local measurements of the synthetic offline retained-artifact
fixture, not a production SLA, capacity estimate or predictive-alpha result.
The run used macOS 26.6.2 ARM64, Python 3.11.16 and SQLite 3.53.4. It ran after the
full test/coverage workloads finished. The timing run did not enable cProfile;
a separate instrumented run identifies hotspots.

## Reproduce

```sh
PYTHONPATH=src:. python benchmarks/final_snapshot.py
PYTHONPATH=src:. python benchmarks/final_snapshot.py --profile /tmp/eie-final.pstats
python -m pstats /tmp/eie-final.pstats
```

The benchmark builds a fresh temporary database, retains native owners, publishes
the catalog, measures queries and validation, closes/reopens the store, then
removes the fixture. Canonical JSON output records elapsed seconds and byte sizes.
The default run measures time without profiling; `--profile` adds instrumentation
and writes its statistics. Instrumented timings should not be compared directly
with unprofiled timings.

## Measured workloads

| Workload | Elapsed |
|---|---:|
| Cross-package fixture build | 2.333 s |
| Full uncached snapshot | 39.20 ms |
| Compact EVENTS snapshot | 16.07 ms |
| Historical symbol binding and snapshot | 28.58 ms |
| Earlier historical cutoff | 4.34 ms |
| Analogue component query | 1.77 ms |
| Persisted typed snapshot lookup | 8.25 ms |
| Cached snapshot with compact revalidation | 35.70 ms |
| Ordered batch, 100 compact requests | 1.800 s |
| Ordered batch, 500 compact requests | 9.086 s |
| Compact validation, full snapshot | 27.40 ms |
| Deep validation, full snapshot | 1.180 s |
| HTTP JSON dispatch | 39.95 ms |
| Close/reopen and historical query | 38.08 ms |

The earlier historical cutoff precedes service publication and consequently has
less component work; it is not an equivalent full-data workload. The component
query selects ANALOGUES. Persisted lookup checks the typed retained payload and
indices; the normal cached service request also recollects and compact-validates
native references. Cache speed therefore does not bypass validation.

Batches use the same compact EVENTS request repeatedly, exercising ordering,
savepoints, persisted reuse and validation. Five hundred full responses would
exceed the default 20 MB aggregate response bound. These measurements do not
represent 500 distinct issuers or concurrent users.

HTTP timing covers strict JSON decoding, service dispatch and response encoding.
It includes a cached full snapshot query and excludes TCP/network transport. It
is not isolated serialization overhead or a transport latency measurement.

## Storage

| Item | Bytes |
|---|---:|
| Full wire snapshot, including computed ID/fingerprint | 84,793 |
| Compact EVENTS wire snapshot | 31,587 |
| Retained service snapshot payloads after workloads | 413,666 |
| Closed SQLite file | 4,272,128 |

Snapshots retain IDs, fingerprints, bounded summaries and provenance rather than
copying upstream documents, graph bodies and study histories. SQLite bytes
include all upstream fixture owners; archive objects are separate and are not
included in this database size. Payload totals are semantic persisted snapshots;
wire views additionally expose the two computed identity fields.

## Hotspots and limits

The separate cProfile run attributes the dominant accumulated work to repeated
batch snapshot compact validation, typed manifest decoding, canonical JSON
serialization and hashing. Deep validation delegates to the upstream auditors
and costs more than compact collection. Accumulated profile times are nested,
so they must not be added together as independent workload totals.

| Profiled operation | Accumulated time |
|---|---:|
| Two batch calls | 36.75 s |
| 606 validations | 29.34 s |
| Canonical JSON serialization | 25.10 s |
| 40,360 typed codec loads | 22.76 s |
| Fingerprint computation | 18.00 s |

The instrumented run recorded 343,759,433 calls (314,251,354 primitive calls) in
48.25 seconds of profiler time. These totals include the entire fixture and all
benchmark stages, and reflect profiler overhead. They support the hotspot
identification, not a service latency promise.

No upstream graph, evidence, analogue, reliability or market-impact algorithm
was rewritten to chase these numbers. Normal requests use indexed publication
and scope/cutoff queries, bounded references and retained artifacts; deep audits
remain explicit. Persisted snapshots are revalidated instead of trusting an
unverified process cache.

The optional HTTP adapter is serial with bounded backlog and connection timeouts.
Timeouts cover connection I/O, not a CPU execution deadline. Request, identity,
reference, batch, expansion and response caps bound admitted work. There is no
claim about throughput, tail latency, remote service reliability or production
readiness under an unmeasured dataset size. Larger catalogs and distinct-symbol
batches require representative profiling and deployment sizing.

See [API limits](service-api.md), [operations](operations.md),
[security](security.md) and [final completion](EIE-17-completion.md).
