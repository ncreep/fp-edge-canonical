from __future__ import annotations

from functools import partial
from logging import Logger
from typing import TYPE_CHECKING, assert_never

from error_types import *
from step2_functional.action_types import ConfigActions
from step2_functional.output_types import *

if TYPE_CHECKING:
    pass
from ops.pebble import Layer

from result import *


def apply_push(
    actions: ConfigActions, logger: Logger, config: PrometheusConf
) -> Result[ApplyError, None]:
    return (
        actions.push_prometheus_config(config.prometheus)
        .then(partial(actions.push_hash, config.config_hash))
        .tap(partial(logger.info, "Pushed new configuration"))
    )


def apply_restart(actions: ConfigActions, layer: Layer) -> Result[ApplyError, None]:
    return actions.update_layer(layer).then(partial(actions.container_replan, layer))


def apply_handler(
    actions: ConfigActions, logger: Logger, outcome: ConfigOutcome
) -> Result[ApplyError, None]:
    match outcome:
        case PushReload(config):
            return apply_push(actions, logger, config).then(actions.reload_config)
        case PushRestart(config, layer):
            return apply_push(actions, logger, config).then(partial(apply_restart, actions, layer))
        case RestartOnly(layer):
            return apply_restart(actions, layer)
        case ContainerOffline():
            return Ok(actions.status_actions.set_maintenance())
        case Noop():
            return Ok(None)
        case _ as unreachable:
            assert_never(unreachable)
