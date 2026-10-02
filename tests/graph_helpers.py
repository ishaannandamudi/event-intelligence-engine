"""Deterministic graph fixtures with foundation identities and raw-first ingestion."""

from dataclasses import replace

import pytest

from eie.graph.models import TraversalPolicy
from eie.graph.repository import GraphRepository
from eie.graph.taxonomy import RelationshipType
from eie.persistence.sqlite import SQLiteEventStore
from eie.providers.relationships.models import RelationshipProvider
from eie.sources.trust import SourceClass
from examples.entity_relationship_graph import (
    DATASET,
    SOURCE,
    fixture_nodes,
)
from examples.entity_relationship_graph import endpoint as endpoint
from examples.entity_relationship_graph import event as event
from examples.entity_relationship_graph import ingest as ingest
from examples.entity_relationship_graph import relationship as relationship


@pytest.fixture
def graph(tmp_path):
    with SQLiteEventStore(tmp_path / "graph.sqlite") as store:
        repo = GraphRepository(store)
        nodes = fixture_nodes()
        for n in nodes:
            repo.append_node(n)
        yield repo, nodes


def provider(name="REL_B", source_class=SourceClass.UNVERIFIED, **kwargs):
    return RelationshipProvider(
        replace(SOURCE, name=name, source_class=source_class),
        DATASET,
        frozenset(RelationshipType),
        **kwargs,
    )


def traversal(*types, **kwargs):
    return TraversalPolicy(tuple(sorted(types or tuple(RelationshipType))), **kwargs)
