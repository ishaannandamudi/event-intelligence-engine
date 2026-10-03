"""Existing SEC history is authoritative; dates preserve explicit source precision."""

from dataclasses import replace

import pytest
from nlp_helpers import CATALOG, ISSUER, artifact, item, source

from eie.entities.models import Security, security_id
from eie.entities.protocols import ResolutionState
from eie.entities.sec import (
    SecEntityRegistry,
    SecIssuerRevision,
    SecListingRevision,
    sec_issuer_id,
    sec_listing_id,
)
from eie.evidence.claims import TimePrecision
from eie.nlp.entities import EntityCatalog
from eie.nlp.extraction import extract
from eie.nlp.schema import EntityMentionKind
from eie.providers.common.models import parse_aware_ns
from eie.providers.sec.models import CIK


@pytest.mark.parametrize(
    "literal,precision,day",
    [
        ("2026-10-01T09:30:00Z", TimePrecision.EXACT, "2026-10-01"),
        ("2026-10-01T09:30:00+02:00", TimePrecision.EXACT, "2026-10-01"),
        ("2026-10-01", TimePrecision.DATE_ONLY, "2026-10-01"),
        ("October 1, 2026", TimePrecision.DATE_ONLY, "2026-10-01"),
        ("October 1", TimePrecision.UNKNOWN, None),
        ("tomorrow", TimePrecision.UNKNOWN, None),
        ("next quarter", TimePrecision.UNKNOWN, None),
        ("2026-02-30", TimePrecision.UNKNOWN, None),
        ("2026-10-01 after market", TimePrecision.DATE_PLUS_SESSION, "2026-10-01"),
        ("2026-10-01 before the open", TimePrecision.DATE_PLUS_SESSION, "2026-10-01"),
        ("2026-10-01 at 09:30", TimePrecision.DATE_ONLY, "2026-10-01"),
    ],
)
def test_precision_from_source(literal, precision, day):
    a = artifact("CIK 0000320193 CEO Jane Smith resigned effective " + literal)
    m = a.prepared.temporal[0]
    assert m.value.precision is precision and m.value.date == day
    if precision is TimePrecision.DATE_PLUS_SESSION:
        assert (
            "market" in a.prepared.normalized.text[m.span.start : m.span.end]
            or "open" in a.prepared.normalized.text[m.span.start : m.span.end]
        )


@pytest.mark.parametrize(
    "timezone,phrase,expected",
    [
        ("America/New_York", "today", "2026-09-30"),
        ("America/New_York", "tomorrow", "2026-10-01"),
        ("UTC", "today", "2026-10-01"),
        (None, "today", None),
    ],
)
def test_relative_time_requires_qualified_publication(timezone, phrase, expected):
    p = replace(source(), timezone=timezone)
    d, b = item(
        p,
        "CIK 0000320193 CEO Jane Smith resigned " + phrase,
        observed=parse_aware_ns("2026-10-02T00:00:00Z", "observed"),
    )
    published = parse_aware_ns("2026-10-01T01:00:00Z", "published")
    d = replace(d, raw=replace(d.raw, published_at_ns=published))
    result = extract(d, b, CATALOG, d.raw.ingested_at_ns + 1)
    assert result.prepared.temporal[0].value.date == expected


def test_names_candidates_people_and_unknown_identifiers():
    a = artifact("Example Corporation CEO Jane Smith resigned; CIK 9999999999 NASDAQ:UNKNOWN")
    kinds = {m.kind for m in a.prepared.entities}
    assert {
        EntityMentionKind.NAME,
        EntityMentionKind.PERSON_ROLE,
        EntityMentionKind.CIK,
        EntityMentionKind.TICKER,
    } <= kinds
    assert all(m.state is not ResolutionState.RESOLVED for m in a.prepared.entities)
    person = next(m for m in a.prepared.entities if m.kind is EntityMentionKind.PERSON_ROLE)
    assert person.identifier == "Jane Smith" and person.role == "CEO"


def test_historical_ticker_validity_knowledge_and_name_ambiguity():
    registry = SecEntityRegistry()
    issuer = CATALOG.issuers[0]
    registry.add_issuer(issuer)
    security = Security(security_id(issuer.issuer_id, "common"), issuer.issuer_id, "Common shares")
    registry.add_security(security)
    listing = SecListingRevision(
        sec_listing_id(security.security_id, "OLD", "NASDAQ"),
        ISSUER,
        security.security_id,
        "OLD",
        "NASDAQ",
        0,
        150,
        0,
        issuer.source_observation_id,
    )
    registry.add_listing(listing)
    snapshot = EntityCatalog.from_registry(registry, (ISSUER,), 100, 100)
    p = source()
    d, b = item(p, "NASDAQ:OLD announced buyback $2B")
    a = extract(d, b, snapshot, 200)
    assert a.prepared.entities[0].state is ResolutionState.RESOLVED
    later = replace(
        d,
        raw=replace(d.raw, source_observed_at_ns=160, ingested_at_ns=160),
        source_lineage=replace(d.source_lineage, observed_at_ns=160, ingested_at_ns=160),
    )
    assert extract(later, b, snapshot, 200).prepared.entities[0].state is ResolutionState.UNRESOLVED
    cik = CIK("0000000001")
    another = SecIssuerRevision(
        sec_issuer_id(cik), cik, issuer.legal_name, (), 0, issuer.source_observation_id
    )
    ambiguous = EntityCatalog(tuple(sorted((issuer, another), key=lambda i: i.issuer_id)))
    named, body = item(p, "Example Corporation CEO Jane Smith resigned")
    n = extract(named, body, ambiguous, 200)
    assert (
        next(m for m in n.prepared.entities if m.kind is EntityMentionKind.NAME).state
        is ResolutionState.AMBIGUOUS
    )
    future = replace(issuer, available_from_ns=300)
    assert (
        extract(d, b, EntityCatalog((future,), ()), 200).prepared.entities[0].state
        is ResolutionState.UNRESOLVED
    )
