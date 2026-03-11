from __future__ import annotations

from functools import partial
from logging import Logger

from apply import ConfigActions, apply_handler
from compute import compute
from errors_handling import handle_errors
from fetch import ConfigSetupFetcher


def process(fetcher: ConfigSetupFetcher, actions: ConfigActions, logger: Logger) -> None:
    apply = partial(apply_handler, actions, logger)
    error_handler = partial(handle_errors, actions.status_actions, logger)

    (
        fetcher.fetch()  #
        .flat_map(compute)
        .flat_map(apply)
        .on_error(error_handler)
    )
