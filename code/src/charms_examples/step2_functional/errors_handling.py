from __future__ import annotations

from logging import Logger
from typing import assert_never

from error_types import *
from step2_functional.action_types import StatusActions

from result import *


def handle_errors(status_actions: StatusActions, logger: Logger, error: ProcessError) -> None:
    match error:
        case FetchError():
            logger.error("Failed to fetch configuration input", exc_info=error)
            status_actions.set_failed_config_gen(error)
        case ConfigErrors():
            logger.error("Failed to generate configuration", exc_info=error)
            status_actions.set_failed_config_gen(error)
        case ConfigPushError():
            logger.error("Failed to push updated config/alert files", exc_info=error)
            status_actions.set_failed_config_push(error)
        case ServiceUpdateError():
            logger.error("Failed to update prometheus service", exc_info=error)
            status_actions.set_failed_update_service(error)
        case ReplanError():
            logger.error("Failed to replan; pebble layer: %s", error.layer, exc_info=error)
            status_actions.set_failed_replan(error, error.layer)
        case ReloadError():
            logger.error("Prometheus failed to reload the configuration", exc_info=error)
            status_actions.set_failed_config_reload()
        case _ as unreachable:
            assert_never(unreachable)
