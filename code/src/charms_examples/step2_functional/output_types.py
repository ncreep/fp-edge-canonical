from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, final

from ops.pebble import Layer


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
