from __future__ import annotations

from typing import TYPE_CHECKING

from error_types import *
from step2_functional.fetch import ConfigSetupFetcher

from result import *

if TYPE_CHECKING:
    from charm import PrometheusCharm
from input_types import *
from step2_functional.charm import CONFIG_HASH_PATH


class _PrometheusConfigSetupFetcher:
    def __init__(self, charm: PrometheusCharm) -> None:
        self._charm = charm

    def fetch(self) -> Result[FetchError, ConfigInput]:
        charm = self._charm

        def build():
            return ConnectedInput(
                charm_name=charm._name,
                config=CharmConfig.model_validate(dict(charm.model.config)),
                scrape_jobs=charm.metrics_consumer.jobs(),
                alert_managers=frozenset(charm.alertmanager_consumer.get_cluster_info()),
                old_config_hash=charm._pull(CONFIG_HASH_PATH) or "",
                current_planned_services=charm.container.get_plan().services,
                command=charm._generate_command(),
                current_services=charm.container.get_services(),
            )

        can_connect = charm.container.can_connect

        return Result.safe(build, FetchError) if can_connect else Ok(DisconnectedInput())


def make_config_fetcher(charm: PrometheusCharm) -> ConfigSetupFetcher:
    return _PrometheusConfigSetupFetcher(charm)

