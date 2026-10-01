"""Small deterministic EIE settings contract."""

from dataclasses import dataclass

from eie.core.errors import ValidationError
from eie.core.fingerprints import fingerprint
from eie.core.schema import (
    CONFIG_SCHEMA_VERSION,
    EVENT_SCHEMA_VERSION,
    OBSERVATION_SCHEMA_VERSION,
    require_schema,
)
from eie.core.time import require_ns
from eie.corporate.settings import CorporateSettings
from eie.expectations.settings import ExpectationsSettings
from eie.providers.common.settings import OfficialProviderSettings
from eie.providers.sec.settings import SecSettings
from eie.scheduled.windows import ScheduledSettings
from eie.sources.trust import SourceClass, SourcePrecedencePolicy


@dataclass(frozen=True, slots=True)
class Settings:
    schema_version: str = CONFIG_SCHEMA_VERSION
    event_schema_version: str = EVENT_SCHEMA_VERSION
    observation_schema_version: str = OBSERVATION_SCHEMA_VERSION
    accepted_source_classes: frozenset[SourceClass] = frozenset(SourceClass)
    default_staleness_ns: int = 86_400_000_000_000
    source_precedence: SourcePrecedencePolicy = SourcePrecedencePolicy()
    strict_replay_availability: bool = True
    scheduled: ScheduledSettings = ScheduledSettings()
    official: OfficialProviderSettings = OfficialProviderSettings()
    sec: SecSettings = SecSettings()
    corporate: CorporateSettings = CorporateSettings()
    expectations: ExpectationsSettings = ExpectationsSettings()

    def __post_init__(self) -> None:
        require_schema(self.schema_version, CONFIG_SCHEMA_VERSION)
        require_schema(self.event_schema_version, EVENT_SCHEMA_VERSION)
        require_schema(self.observation_schema_version, OBSERVATION_SCHEMA_VERSION)
        if (
            not isinstance(self.accepted_source_classes, frozenset)
            or not self.accepted_source_classes
            or any(not isinstance(item, SourceClass) for item in self.accepted_source_classes)
        ):
            raise ValidationError("at least one source class required")
        if not isinstance(self.source_precedence, SourcePrecedencePolicy):
            raise ValidationError("typed immutable source precedence required")
        if not isinstance(self.scheduled, ScheduledSettings):
            raise ValidationError("typed immutable scheduled settings required")
        if not isinstance(self.official, OfficialProviderSettings):
            raise ValidationError("typed immutable official provider settings required")
        if not isinstance(self.sec, SecSettings):
            raise ValidationError("typed immutable SEC settings required")
        if not isinstance(self.corporate, CorporateSettings):
            raise ValidationError("typed immutable corporate settings required")
        if not isinstance(self.expectations, ExpectationsSettings):
            raise ValidationError("typed immutable expectations settings required")
        require_ns(self.default_staleness_ns, "default_staleness_ns")
        if self.strict_replay_availability is not True:
            raise ValidationError("EIE-0 cannot disable causal replay validation")

    @property
    def fingerprint(self) -> str:
        return fingerprint("eie:configuration:v1", self)
