#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import hashlib
import logging
import re
import socket
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Optional, Tuple, TypedDict, Union, cast

import yaml
from cosl.time_validation import is_valid_timespec
from lightkube.core.client import Client
from lightkube.core.exceptions import ApiError as LightkubeApiError
from lightkube.resources.core_v1 import PersistentVolumeClaim, Pod
from lightkube.utils.quantity import parse_quantity
from ops import StoredState
from ops.charm import CharmBase
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    MaintenanceStatus,
    ModelError,
    StatusBase,
)
from ops.pebble import Error as PebbleError
from ops.pebble import Layer

from charms.alertmanager_k8s.v1.alertmanager_dispatch import AlertmanagerConsumer
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointConsumer, PrometheusConfig
from charms.tls_certificates_interface.v4.tls_certificates import (
    CertificateRequestAttributes,
    TLSCertificatesRequiresV4,
)
from charms.traefik_k8s.v1.ingress_per_unit import IngressPerUnitRequirer
from prometheus_client import Prometheus

PROMETHEUS_DIR = "/etc/prometheus"
PROMETHEUS_CONFIG = f"{PROMETHEUS_DIR}/prometheus.yml"
CONFIG_HASH_PATH = f"{PROMETHEUS_DIR}/config.sha256"

KEY_PATH = f"{PROMETHEUS_DIR}/server.key"
CERT_PATH = f"{PROMETHEUS_DIR}/server.cert"
WEB_CONFIG_PATH = f"{PROMETHEUS_DIR}/prometheus-web-config.yml"

EXEMPLARS_FLOOR = 100000

logger = logging.getLogger(__name__)


def to_tuple(status: StatusBase) -> Tuple[str, str]:
    return status.name, status.message


_STATUS_ACTIVE = to_tuple(ActiveStatus())
_STATUS_MAINTENANCE_CONFIGURING = to_tuple(MaintenanceStatus("Configuring Prometheus"))
_STATUS_CFG_LOAD_FAIL = to_tuple(
    BlockedStatus("Prometheus failed to reload the configuration; see debug logs")
)
_STATUS_RESTART_FAIL = to_tuple(
    BlockedStatus("Prometheus failed to restart (config valid?); see debug logs")
)
_STATUS_PUSH_FAIL = to_tuple(
    BlockedStatus("Failed to push updated config/alert files; see debug logs")
)
_STATUS_LAYER_FAIL = to_tuple(BlockedStatus("Failed to update prometheus service; see debug logs"))


def blocked_status(message: str) -> Tuple[str, str]:
    return to_tuple(BlockedStatus(message))


def sha256(hashable) -> str:
    if isinstance(hashable, str):
        hashable = hashable.encode("utf-8")
    return hashlib.sha256(hashable).hexdigest()


class ConfigError(Exception):
    pass


class CompositeStatus(TypedDict):
    retention_size: Tuple[str, str]
    config: Tuple[str, str]


@dataclass
class TLSConfig:
    server_cert: str
    ca_cert: str
    private_key: str


class PrometheusCharm(CharmBase):
    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self._fqdn = socket.getfqdn()
        self._stored.set_default(
            status=CompositeStatus(
                retention_size=_STATUS_ACTIVE,
                config=_STATUS_ACTIVE,
            )
        )

        self._name = "prometheus"
        self._port = 9090
        self.container = self.unit.get_container(self._name)

        self._csr_attributes = CertificateRequestAttributes(
            common_name=self.app.name,
            sans_dns=frozenset((self._fqdn,)),
        )
        self._cert_requirer = TLSCertificatesRequiresV4(
            charm=self,
            relationship_name="certificates",
            certificate_requests=[self._csr_attributes],
        )

        self.ingress = IngressPerUnitRequirer(
            self,
            relation_name="ingress",
            port=self._port,
            strip_prefix=True,
            redirect_https=True,
            scheme=lambda: "https" if self._tls_available else "http",
        )

        self.alertmanager_consumer = AlertmanagerConsumer(
            charm=self,
            relation_name="alertmanager",
        )

        self.metrics_consumer = MetricsEndpointConsumer(self)

        self._prometheus_client = Prometheus(self.internal_url)

        self.framework.observe(self.on.config_changed, self._configure)

    @property
    def _prometheus_layer(self) -> Layer:
        return Layer(
            {
                "summary": "Prometheus layer",
                "description": "Pebble layer configuration for Prometheus",
                "services": {
                    self._name: {
                        "override": "replace",
                        "summary": "prometheus daemon",
                        "command": self._generate_command(),
                        "startup": "enabled",
                    }
                },
            }
        )

    def _configure(self, _):
        if not self.container.can_connect():
            self._stored.status["config"] = _STATUS_MAINTENANCE_CONFIGURING
            return

        try:
            should_reload = self._generate_prometheus_config()
        except ConfigError as e:
            logger.error("Failed to generate configuration: %s", e)
            self._stored.status["config"] = blocked_status(str(e))
            return
        except PebbleError as e:
            logger.error("Failed to push updated config/alert files: %s", e)
            self._stored.status["config"] = _STATUS_PUSH_FAIL
            return

        try:
            layer_changed = self._update_layer()
        except PebbleError as e:
            logger.error("Failed to update prometheus service: %s", e)
            self._stored.status["config"] = _STATUS_LAYER_FAIL
            return

        try:
            if layer_changed:
                self.container.replan()
                logger.info("Prometheus (re)started")
        except PebbleError as e:
            logger.error(
                "Failed to replan; pebble layer: %s; %s",
                self._prometheus_layer.to_dict(),
                e,
            )
            self._stored.status["config"] = _STATUS_RESTART_FAIL
            return

        if not layer_changed and should_reload:
            reloaded = self._prometheus_client.reload_configuration()
            if not reloaded:
                logger.error("Prometheus failed to reload the configuration")
                self._stored.status["config"] = _STATUS_CFG_LOAD_FAIL
                return

            logger.info("Prometheus configuration reloaded")

        self._stored.status["config"] = _STATUS_ACTIVE

    def _update_layer(self) -> bool:
        current_planned_services = self.container.get_plan().services
        new_layer = self._prometheus_layer

        current_services = self.container.get_services()  # mapping from str to ServiceInfo
        all_svcs_running = all(svc.is_running() for svc in current_services.values())
        services_unchanged = current_planned_services == new_layer.services

        if services_unchanged and all_svcs_running:
            return False

        self.container.add_layer(self._name, new_layer, combine=True)
        return True

    def _generate_prometheus_config(self) -> bool:
        prometheus_config = {
            "global": {
                "scrape_interval": "1m",
                "scrape_timeout": "10s",
            },
            "scrape_configs": [],
        }

        self._configure_evaluation_interval(prometheus_config)
        self._configure_alert_managers(prometheus_config)
        self._configure_scrape_jobs(prometheus_config)

        config_hash = sha256(yaml.safe_dump(prometheus_config))

        if config_hash == self._pull(CONFIG_HASH_PATH):
            return False

        self._push(PROMETHEUS_CONFIG, yaml.safe_dump(prometheus_config))
        self._push(CONFIG_HASH_PATH, config_hash)

        logger.info("Pushed new configuration")
        return True

    def _configure_evaluation_interval(self, prometheus_config: dict) -> None:
        evaluation_interval = self.model.config.get("evaluation_interval")
        if (
            evaluation_interval
            and isinstance(evaluation_interval, str)
            and is_valid_timespec(evaluation_interval)
        ):
            prometheus_config["global"]["evaluation_interval"] = evaluation_interval
        else:
            raise ConfigError(f"Invalid evaluation_interval: [{evaluation_interval}]")

    def _configure_alert_managers(self, prometheus_config: dict) -> None:
        alert_managers = self.alertmanager_consumer.get_cluster_info()
        if not alert_managers:
            raise ConfigError("No alertmanagers available")

        scheme = cast(str, self.model.config["alertmanager_scheme"])
        if scheme not in ("http", "https"):
            raise ConfigError(f"Invalid alertmanager_scheme: [{scheme}]")

        alerting = PrometheusConfig.render_alertmanager_static_configs(list(alert_managers))
        for am in alerting["alertmanagers"]:
            am["scheme"] = scheme

        prometheus_config["alerting"] = alerting

    def _configure_scrape_jobs(self, prometheus_config: dict) -> None:
        for job in self._get_metrics_jobs():
            job["honor_labels"] = cast(bool, self.model.config["honor_labels"])

            self._process_tls_config(job)
            prometheus_config["scrape_configs"].append(job)

    def _get_metrics_jobs(self) -> list[dict[str, Any]]:
        return self.metrics_consumer.jobs()

    def _process_tls_config(self, job) -> None:
        def with_dir_prefix(name: str):
            return f"{PROMETHEUS_DIR}/{job['job_name']}-{name}"

        if tls_config := job.get("tls_config", {}):
            if (cert_file := tls_config.get("cert_file")) and (
                key_file := tls_config.get("key_file")
            ):
                filename = with_dir_prefix("client.crt")
                job["tls_config"]["cert_file"] = filename
                job["tls_config"]["cert_file_content"] = cert_file

                filename = with_dir_prefix("client.key")
                job["tls_config"]["key_file"] = filename
                job["tls_config"]["key_file_content"] = key_file
            elif "cert_file" in tls_config or "key_file" in tls_config:
                raise ConfigError(
                    'tls_config requires both "cert_file" and "key_file" if client '
                    "authentication is to be used"
                )

    def _pull(self, path) -> Optional[str]:
        try:
            return cast(str, self.container.pull(path, encoding="utf-8").read())
        except (FileNotFoundError, PebbleError):
            # Drop FileNotFoundError https://github.com/canonical/operator/issues/896
            return None

    def _push(self, path, contents):
        self.container.push(path, contents, make_dirs=True, encoding="utf-8")

    @property
    def _exemplars(self) -> int:
        exemplars_from_config = cast(
            int, self.model.config.get("max_global_exemplars_per_user", 0)
        )
        if exemplars_from_config > 0:
            return max(exemplars_from_config, EXEMPLARS_FLOOR)
        return 0

    def _percent_string_to_ratio(self, percentage: str) -> float:
        if not percentage.endswith("%"):
            raise ValueError("Must be a number followed by '%', e.g. '80%'")
        value = float(percentage[:-1]) / 100.0
        if value < 0 or value > 1:
            raise ValueError("Percentage value must be in the range 0-100.")
        return value

    def _get_pvc_capacity(self) -> str:
        assert "database" in self.model.storages, (
            "The 'database' storage is no longer in metadata: must update literals in charm code."
        )

        client = Client()  # pyright: ignore
        pod_name = self.unit.name.replace("/", "-", -1)

        pvc_name = ""
        for volume in cast(
            Pod, client.get(Pod, name=pod_name, namespace=self.model.name)
        ).spec.volumes:  # pyright: ignore
            if not volume.persistentVolumeClaim:
                continue
            matcher = re.compile(rf"^{self.app.name}-database-.*?-{pod_name}$")
            if matcher.match(volume.persistentVolumeClaim.claimName):
                pvc_name = volume.persistentVolumeClaim.claimName
                break

        if not pvc_name:
            raise ValueError("No PVC found for pod " + pod_name)

        namespace_file = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
        if namespace_file.exists():
            namespace = namespace_file.read_text().strip()
        else:
            namespace = self.model.name

        capacity = cast(
            PersistentVolumeClaim,
            client.get(PersistentVolumeClaim, name=pvc_name, namespace=namespace),
        ).status.capacity[  # pyright: ignore
            "storage"
        ]

        return capacity

    def _generate_command(self) -> str:
        config = self.model.config
        args = [
            f"--config.file={PROMETHEUS_CONFIG}",
            "--storage.tsdb.path=/var/lib/prometheus",
            "--web.enable-lifecycle",
            "--web.console.templates=/usr/share/prometheus/consoles",
            "--web.console.libraries=/usr/share/prometheus/console_libraries",
        ]

        if self._web_config():
            args.append(f"--web.config.file={WEB_CONFIG_PATH}")

        external_url = self.most_external_url.rstrip("/")
        args.append(f"--web.external-url={external_url}")
        args.append("--web.route-prefix=/")

        args.append("--web.enable-remote-write-receiver")

        args.append(f"--log.level={self.log_level}")

        if config.get("metrics_wal_compression"):
            args.append("--storage.tsdb.wal-compression")

        if self._exemplars:
            args.append("--enable-feature=exemplar-storage")

        if is_valid_timespec(
            retention_time := cast(str, config.get("metrics_retention_time", ""))
        ):
            args.append(f"--storage.tsdb.retention.time={retention_time}")

        try:
            ratio = self._percent_string_to_ratio(
                cast(str, config.get("maximum_retention_size", ""))
            )

        except ValueError as e:
            logger.warning(e)
            self._stored.status["retention_size"] = blocked_status(
                f"Invalid retention size: {e}, only metrics_retention_time is in effect"
            )

        else:
            try:
                capacity = convert_k8s_quantity_to_legacy_binary_gigabytes(
                    self._get_pvc_capacity(), ratio
                )
            except ValueError as e:
                self._stored.status["retention_size"] = blocked_status(
                    f"Error calculating retention size: {e}"
                )
            except LightkubeApiError as e:
                self._stored.status["retention_size"] = blocked_status(
                    f"Error calculating retention size "
                    f"(try running `juju trust` on this application): {e}"
                )
            else:
                logger.debug("Retention size limit set to %s (%s%%)", capacity, ratio * 100)
                args.append(f"--storage.tsdb.retention.size={capacity}")
                self._stored.status["retention_size"] = _STATUS_ACTIVE

        command = ["/bin/prometheus"] + args

        return " ".join(command)

    @property
    def internal_url(self) -> str:
        scheme = "https" if self._tls_available else "http"
        return f"{scheme}://{self._fqdn}:{self._port}"

    @property
    def external_url(self) -> Optional[str]:
        try:
            if ingress_url := self.ingress.url:
                return ingress_url
        except ModelError as e:
            logger.error("Failed obtaining external url: %s. Shutting down?", e)
        return None

    @property
    def most_external_url(self) -> str:
        external_url = self.external_url
        if external_url:
            return external_url

        return self.internal_url

    @property
    def log_level(self):
        allowed_log_levels = ["debug", "info", "warn", "error", "fatal"]
        log_level = cast(str, self.model.config["log_level"]).lower()

        if log_level not in allowed_log_levels:
            logging.warning(
                "Invalid loglevel: %s given, %s allowed. defaulting to DEBUG loglevel.",
                log_level,
                "/".join(allowed_log_levels),
            )
            log_level = "debug"
        return log_level

    @property
    def _tls_config(self) -> Optional[TLSConfig]:
        certificates, key = self._cert_requirer.get_assigned_certificate(
            certificate_request=self._csr_attributes
        )

        if not (key and certificates):
            return None
        return TLSConfig(certificates.certificate.raw, certificates.ca.raw, key.raw)

    @property
    def _tls_available(self) -> bool:
        return bool(self._tls_config)

    def _web_config(self) -> Optional[dict]:
        if self._tls_available:
            return {
                "tls_server_config": {
                    "cert_file": CERT_PATH,
                    "key_file": KEY_PATH,
                }
            }
        return None


def convert_k8s_quantity_to_legacy_binary_gigabytes(
    capacity: str, multiplier: Union[Decimal, float, str] = 1.0
) -> str:
    if not isinstance(multiplier, Decimal):
        try:
            multiplier = Decimal(multiplier)
        except ArithmeticError as e:
            raise ValueError("Invalid multiplier") from e

    if not multiplier.is_finite():
        raise ValueError("Multiplier must be finite")

    if not (capacity_as_decimal := parse_quantity(capacity)):
        raise ValueError(f"Invalid capacity value: {capacity}")

    storage_value = multiplier * capacity_as_decimal / 2**30
    quantized = storage_value.quantize(Decimal("0.001"))
    as_str = str(quantized).rstrip("0").rstrip(".")
    return f"{as_str}GB"
