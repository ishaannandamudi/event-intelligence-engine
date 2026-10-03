# Bitemporal intelligence

EIE-16 adds a descriptive audit over explicitly indexed, retained source revisions.
It preserves source/evidence/canonical ownership and does not select a new canonical truth.

`ReliabilityQueries.audit(dataset, subject, known_as_of_ns, valid_at_ns)` returns an
immutable `BitemporalAuditResult`. Subjects may be semantic claim IDs, provider claim
thread IDs, exact assertion IDs, canonical event IDs/revision IDs, or raw observation
IDs. Index each needed owner revision before querying; an absent index is not a claim
that the source universe contains no events.

Valid time says when a sourced fact applies. Claim validity uses existing half-open
`valid_from_ns`/`valid_to_ns`. Canonical validity uses the explicit effective clock;
publication, announcement and schedule are not substituted for missing validity.
Coarse validity retains its supplied calendar date/session and cannot imply an exact instant.
Unknown validity is returned separately, never treated as universally applicable.
Within each provider thread the greatest applicable visible revision is retained.
A later unknown-validity revision makes an older otherwise-applicable selection ambiguous.
Retraction and correction remain information actions visible in the timeline.

Knowledge time records the original owner's `known_from_ns` or revision availability.
The audit selects the latest diagnostic frame locally available by the query cutoff;
a source-only frame preserves visibility before later instrumentation arrives. Entries retain actual observed time and local diagnostic availability.
An EIE-7 reconstructed source clock may predate local acquisition; it never becomes an
old local observation. EIE-7/EIE-11 reconstructed-source views remain in their owners.
EIE-16's local audit is conservative until acquisition/processing evidence is available.

Timeline entries retain exact row/revision IDs, fingerprints through those references,
revision numbers, actions, valid bounds and knowledge clocks. No payload or raw text is
copied. Multiple providers yield multiple selections; no empirical metric resolves them.

Saved audits, latency and reliability results are immutable. New corrections and policy
versions yield new identities. Collection and persistence share one SQLite transaction.
Index cutoffs are sealed: a new row cannot be backdated into a query that could see it.
Honestly acquired later backfills remain admissible and excluded from old local views.
