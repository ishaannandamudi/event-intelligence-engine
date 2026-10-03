# NLP security boundary

Source text is inert data. Model output is a proposal. Neither can execute commands,
load a URL, choose a filesystem path, change provider configuration, grant retention,
load a runtime class, redefine the schema or become source authority.

The runtime uses bounded Unicode/HTML parsing, static classification patterns,
strict plain JSON and fixed enums/dataclasses. There is no source-triggered shell,
filesystem, browser, network, dynamic import, eval, pickle or tool dispatch. The
model receives only typed instructions/settings and isolated source data. Model
adapters are caller-supplied trusted code, not an execution facility for source text.

Prompt separation is structural, with fixed trusted instructions and output schema.
Defense does not depend on a delimiter string staying absent from source text.
Instruction-shaped segments are excluded from factual acceptance conservatively;
this heuristic is an additional recall restriction, not a guarantee about every
possible linguistic injection. Numeric, identity, span, precision, context and
status verification remain mandatory even if a model ignores instructions.

## Explicit adversarial checks

Offline fixtures include ignored-instruction attacks, fake system/developer text,
fake JSON tool calls, shell instructions, credential requests, external URL commands,
stock-buy directives and schema redefinitions. They stay data and cannot be accepted
as instructions. Output attacks include unknown actions, nested objects, missing
spans, wrong primitives, duplicate fields, excessive arrays, unsafe keys and nonfinite
numbers. No source or response can supply a persisted class name for dynamic import.

Accepted facts always point to exact original source spans. Model-only facts,
unsupported numeric/metric/period bindings, precision upgrades and unresolved
issuers cannot bypass EIE-11. Denials, negation, plans, allegations, rumor and
conditions cannot become confirmed occurrences. High categorical extraction quality
does not improve source class, corroboration or credibility.

## Persistence and operations

One owned SQLite transaction covers accepted claims, source checkpoint, text
artifacts, manifest, span links and accepted links. Failures roll back the entire
item. Foreign keys, immutable triggers, typed payload identities, scalar indexes,
complete memberships and source-backed recalculation detect corruption. Queries
fail on bounds rather than silently returning incomplete results.

No exception message, model credential or source body is logged by the extraction
pipeline. Fixed failure categories and hashes are sufficient diagnostics. Raw
content is retained only with configured permission and bounds; metadata-only
queries cannot recover a body stored under another provider's entitlement.
