from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Literal, Mapping, Sequence, final

from ops.pebble import Service, ServiceInfo
from pydantic import BaseModel, ConfigDict


@final
class CharmConfig(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    evaluation_interval: str
    alertmanager_scheme: Literal["http", "https"]
    honor_labels: bool


@final
@dataclass(frozen=True)
class ConnectedInput:
    charm_name: str
    config: CharmConfig
    scrape_jobs: Sequence[Mapping[str, Any]]
    alert_managers: FrozenSet[str]
    old_config_hash: str
    current_planned_services: Mapping[str, Service]
    command: str
    current_services: Mapping[str, ServiceInfo]


@final
@dataclass(frozen=True)
class DisconnectedInput:
    pass


type ConfigInput = ConnectedInput | DisconnectedInput
