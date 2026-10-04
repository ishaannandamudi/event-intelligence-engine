# Provider-neutral downstream contract

`eie.integration.contracts.EventIntelligenceProvider` is the downstream boundary. The bundled `EventIntelligenceService` satisfies it. Consumers use typed snapshots, selected components and bounded expansion; they do not require knowledge of EIE SQLite tables or ingestion providers. There are no sibling package imports or runtime dependencies.

```python
from pathlib import Path
from tempfile import TemporaryDirectory
from eie.integration.contracts import EventIntelligenceProvider
from eie.service.schema import ComponentKind
from examples.final_snapshot import build

with TemporaryDirectory() as directory:
    scenario = build(Path(directory))
    provider: EventIntelligenceProvider = scenario.service
    snapshot = provider.snapshot(scenario.snapshot.request)
    for component in snapshot.components:
        print(component.kind, component.state, component.completeness)
        if component.kind in (ComponentKind.IMPACT, ComponentKind.INTELLIGENCE):
            for reference in component.references:
                detail = provider.expand(snapshot.snapshot_id, reference.owner_id)
    scenario.store.close()
```

Event state, information surprise, novelty, static importance, materiality, uncertainty, analogue distributions, descriptive studies, model-specific impact, interaction categories, source class and empirical diagnostics keep their established owners. No combined confidence, importance, reliability or severity score is introduced. Selecting components changes the final request/result identity, while native artifact IDs/fingerprints remain unchanged.

A future Alpha Forecast & Research Engine can retain snapshot ID, cutoff, version matrix and native references alongside its own research inputs. Forecast targets, calibration, portfolio decisions and trading actions belong to that downstream system. EIE provides event intelligence infrastructure; it does not prove a profitable strategy.

## Publishing adapter responsibility

Trusted application code supplies a catalog ID and an explicit service publication clock to `ServiceRepository.publish(catalog, owner_kind, exact_native_lookup, available_ns)`. The bridge verifies the retained owner's type, fingerprint, native indices/dependencies and compatible versions. Publication cannot precede native acquisition/extraction/build availability. `retain_expectation` retains the established expectations engine's surprise/consensus query, verifies it against its persisted session and references the native source observations.

Catalog publication is a separate clock from source knowledge. Retrospective EIE-7 builds preserve their original source clocks and actual later build availability. Publishing a historical artifact today does not insert it into an earlier live snapshot. A frozen catalog cutoff seals all components, including missing ones; later registration at or before that cutoff is refused. Use a new catalog/build lineage for a genuinely different reconstructed view.

Only explicit trusted `CoverageDeclaration` evidence can establish completeness/known-empty. A consumer should inspect both component state and completeness before treating an absent event as meaningful. MARKET queries cover that catalog's published universe, not all world events.

See [API](service-api.md), [operations](operations.md), [compatibility](compatibility.md), and [replay](replay-and-causality.md).
