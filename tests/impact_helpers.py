"""Small frozen historical build and supplied counterfactual inputs."""

from eie.impact.engine import ImpactEngine
from eie.impact.models import ModelType
from examples.counterfactual_impact import CARS, ESTIMATION, fixture
from examples.event_studies import history


def inputs(root, *, method=None):
    store, reader, ids = history(root)
    values = fixture(reader, ids[-1], **({} if method is None else {"method": method}))
    return store, reader, values


def impact(base, kind=ModelType.MARKET_MODEL, **kwargs):
    return ImpactEngine().impact_event(base, kind, ESTIMATION, CARS, **kwargs)
