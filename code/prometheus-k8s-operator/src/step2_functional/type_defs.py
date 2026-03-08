from __future__ import annotations

from dataclasses import dataclass
from typing import Any, FrozenSet, Literal, Mapping, Sequence, final

from ops.pebble import Layer, Service, ServiceInfo
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


@final
@dataclass(frozen=True)
class PushReload:
    config: PrometheusConf


@final
@dataclass(frozen=True)
class PushRestart:
    config: PrometheusConf
    layer: Layer


@final
@dataclass(frozen=True)
class RestartOnly:
    layer: Layer


@final
@dataclass(frozen=True)
class Noop:
    pass


@final
@dataclass(frozen=True)
class ContainerOffline:
    pass


type ConfigOutcome = PushReload | PushRestart | RestartOnly | ContainerOffline | Noop


@final
@dataclass(frozen=True)
class PrometheusConf:
    prometheus: Mapping
    config_hash: str


@final
@dataclass(frozen=True)
class NoReload:
    pass


@final
@dataclass(frozen=True)
class ReloadConf:
    config: PrometheusConf


type PrometheusReloadStatus = ReloadConf | NoReload


@final
@dataclass(frozen=True)
class NoRestart:
    pass


@final
@dataclass(frozen=True)
class RestartLayer:
    layer: Layer


type LayerRestartStatus = RestartLayer | NoRestart


class FetchError(Exception):
    pass


class ConfigError(Exception):
    pass


class ConfigErrors(ExceptionGroup[ConfigError]):
    pass


class ConfigPushError(Exception):
    pass


class ServiceUpdateError(Exception):
    pass


class ReplanError(Exception):
    def __init__(self, layer: Layer, cause: BaseException) -> None:
        self.__cause__ = cause
        self.layer = layer


class ReloadError(Exception):
    pass


class ConfigTimeoutError(Exception):
    pass


type ApplyError = ConfigPushError | ServiceUpdateError | ReplanError | ReloadError

type ProcessError = FetchError | ConfigErrors | ApplyError
