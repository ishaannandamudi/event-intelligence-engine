# Unscheduled events

EIE-12 adds source-backed text intelligence, schema `1.0`. The closed extraction
families align with existing event taxonomy:

| Extraction family | Existing event family |
| --- | --- |
| Earnings result, guidance change | EARNINGS |
| Management change, bankruptcy/restructuring | MANAGEMENT_GOVERNANCE |
| M&A, capital raise, buyback, dividend, ownership | CORPORATE_ACTION |
| Legal/regulatory, FDA decision, rating action | REGULATORY_LEGAL |
| Cybersecurity, product, contract, supply chain, operational disruption, recall, activist | PRODUCT_BUSINESS |
| Other explicit unscheduled announcement | OTHER |

One document can produce many candidates. The offline example separates earnings,
buyback and CEO departure while preserving common document/raw provenance. Multiple
management changes within explicit clauses retain distinct person, role, action and
effective identities. Repeated complete identities can share a source-local event.
Other repeated family cues and competing period/context bindings remain ambiguous;
the baseline does not identify every distinct narrative event.

## Epistemic status

`REPORTED` describes a source assertion, not independent confirmation. `RUMORED`,
`DENIED`, `NEGATED`, `PLANNED`, `EXPECTED`, `CONDITIONAL` and `ALLEGED` preserve their
source context as unaccepted/partial artifacts. Quoted allegations do not become
author-confirmed facts. A guidance forecast can establish issuance of guidance,
never realization of the forecast. An acquisition announcement never sets `CLOSED`
or an actual close time. The source-local event lifecycle describes publication of
an event report; it is not a deal-completion decision.

Management candidates retain explicit role/person/action/effective date. M&A
candidates can retain explicit target/acquirer CIK fields, consideration and qualified
expected-close date, with ambiguity kept visible. Legal/regulatory extraction retains
explicit agency/action and amount; cyber extraction retains explicit operational
impact. Product/contract/supply events preserve resolved party mentions without
creating permanent graph relationships or inferring severity or importance.

## Resolution boundary and limitations

EIE-12 extracts each source independently. EIE-11 owns multi-source corroboration,
source independence, numeric conflicts, conservative event aliases and revision
relations. Same content can share bytes while provider observations remain distinct.
Mirrors should share a configured source family to avoid extra independent votes.

Baseline language is explicitly English. Grammar is deliberately restricted: exact
financial literals, explicit fiscal period ends, literal roles/actions, exact
identifiers and supported timing phrases. Names never resolve finally by themselves.
There is no arbitrary table interpretation, semantic prose understanding, people
graph, generic legal reasoning, vendor model transport or calibrated extraction
probability. Unsupported or uncertain details remain auditable rather than fabricated.
Novelty, materiality, analogues, collision modeling, service/API, integrations,
sentiment signals and trading are deferred.
