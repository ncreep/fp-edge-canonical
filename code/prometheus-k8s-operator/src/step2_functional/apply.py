from __future__ import annotations

from functools import partial
from logging import Logger
from typing import TYPE_CHECKING, Mapping, Protocol, assert_never

import yaml
from errors import StatusActions

from step2_functional.charm import CONFIG_HASH_PATH, PROMETHEUS_CONFIG

if TYPE_CHECKING:
    from charm import PrometheusCharm
from ops.model import ActiveStatus, BlockedStatus, MaintenanceStatus, StatusBase
from ops.pebble import Layer
from result import *
from type_defs import *


class ConfigActions(Protocol):
    @property
    def status_actions(self) -> StatusActions: ...

    def push_prometheus_config(
        self, prometheus_config: Mapping
    ) -> Result[ConfigPushError, None]: ...

    def push_hash(self, hash: str) -> Result[ConfigPushError, None]: ...

    def reload_config(self) -> Result[ReloadError, None]: ...

    def update_layer(self, layer: Layer) -> Result[ServiceUpdateError, None]: ...

    def container_replan(self, layer: Layer) -> Result[ReplanError, None]: ...


def _apply_push(
    actions: ConfigActions, logger: Logger, config: PrometheusConf
) -> Result[ApplyError, None]:
    return (
        actions.push_prometheus_config(config.prometheus)
        .then(partial(actions.push_hash, config.config_hash))
        .tap(partial(logger.info, "Pushed new configuration"))
    )


def _apply_restart(actions: ConfigActions, layer: Layer) -> Result[ApplyError, None]:
    return actions.update_layer(layer).then(partial(actions.container_replan, layer))


def apply_handler(
    actions: ConfigActions, logger: Logger, outcome: ConfigOutcome
) -> Result[ApplyError, None]:
    match outcome:
        case PushReload(config):
            return _apply_push(actions, logger, config).then(actions.reload_config)
        case PushRestart(config, layer):
            return _apply_push(actions, logger, config).then(
                partial(_apply_restart, actions, layer)
            )
        case RestartOnly(layer):
            return _apply_restart(actions, layer)
        case ContainerOffline():
            return Ok(actions.status_actions.set_maintenance())
        case Noop():
            return Ok(None)
        case _ as unreachable:
            assert_never(unreachable)


class _PrometheusStatusActions:
    def __init__(self, charm: PrometheusCharm) -> None:
        self._charm = charm

    def set_active(self) -> None:
        self._charm._stored.status["config"] = to_tuple(ActiveStatus())

    def set_maintenance(self) -> None:
        self._charm._stored.status["config"] = to_tuple(
            MaintenanceStatus("Configuring Prometheus")
        )

    def set_failed_config_gen(self, e: Exception) -> None:
        self._charm._stored.status["config"] = to_tuple(BlockedStatus(str(e)))

    def set_failed_config_push(self, e: Exception) -> None:
        self._charm._stored.status["config"] = to_tuple(
            BlockedStatus("Failed to push updated config/alert files; see debug logs")
        )

    def set_failed_update_service(self, e: Exception) -> None:
        self._charm._stored.status["config"] = to_tuple(
            BlockedStatus("Failed to update prometheus service; see debug logs")
        )

    def set_failed_replan(self, e: Exception, layer: Layer) -> None:
        self._charm._stored.status["config"] = to_tuple(
            BlockedStatus("Prometheus failed to restart (config valid?); see debug logs")
        )

    def set_failed_config_reload(self) -> None:
        self._charm._stored.status["config"] = to_tuple(
            BlockedStatus("Prometheus failed to reload the configuration; see debug logs")
        )


class _PrometheusConfigActions:
    def __init__(self, charm: PrometheusCharm) -> None:
        self._charm = charm
        self._status = _PrometheusStatusActions(charm)

    @property
    def status_actions(self) -> StatusActions:
        return self._status

    def push_prometheus_config(self, prometheus_config: Mapping) -> Result[ConfigPushError, None]:
        return Result.safe(
            partial(self._charm._push, PROMETHEUS_CONFIG, yaml.safe_dump(dict(prometheus_config))),
            ConfigPushError,
        )

    def push_hash(self, hash: str) -> Result[ConfigPushError, None]:
        return Result.safe(
            partial(self._charm._push, CONFIG_HASH_PATH, hash),
            ConfigPushError,
        )

    def reload_config(self) -> Result[ReloadError, None]:
        reloaded = self._charm._prometheus_client.reload_configuration()
        return Ok(None) if reloaded is True else Err(ReloadError())

    def update_layer(self, layer: Layer) -> Result[ServiceUpdateError, None]:
        return Result.safe(
            partial(self._charm.container.add_layer, self._charm._name, layer, combine=True),
            ServiceUpdateError,
        )

    def container_replan(self, layer: Layer) -> Result[ReplanError, None]:
        return Result.safe(self._charm.container.replan, partial(ReplanError, layer))


def make_config_actions(charm: PrometheusCharm) -> ConfigActions:
    return _PrometheusConfigActions(charm)


def to_tuple(status: StatusBase) -> tuple[str, str]:
    return status.name, status.message
