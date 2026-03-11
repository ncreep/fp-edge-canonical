#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import re
import socket
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Optional, Tuple, TypedDict, Union, cast

from apply import make_config_actions
from cosl.time_validation import is_valid_timespec
from fetch import make_config_fetcher
from lightkube.core.client import Client
from lightkube.core.exceptions import ApiError as LightkubeApiError
from lightkube.resources.core_v1 import PersistentVolumeClaim, Pod
from lightkube.utils.quantity import parse_quantity
from ops import StoredState
from ops.charm import CharmBase
from ops.model import (
    ActiveStatus,
    BlockedStatus,
    ModelError,
    StatusBase,
)
from ops.pebble import Error as PebbleError
from pipeline import process

from charms.alertmanager_k8s.v1.alertmanager_dispatch import AlertmanagerConsumer
from charms.prometheus_k8s.v0.prometheus_scrape import MetricsEndpointConsumer
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


def blocked_status(message: str) -> Tuple[str, str]:
    return to_tuple(BlockedStatus(message))


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

    def _configure(self, _):
        process(make_config_fetcher(self), make_config_actions(self), logger)

    def _pull(self, path) -> Optional[str]:
        try:
            return cast(str, self.container.pull(path, encoding="utf-8").read())
        except (FileNotFoundError, PebbleError):
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
