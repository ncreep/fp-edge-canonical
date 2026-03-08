#!/usr/bin/env python3

# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""A Juju charm for Prometheus on Kubernetes."""

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

# Paths for the private key and the signed server certificate.
# These are used to present to clients and to authenticate other servers.
KEY_PATH = f"{PROMETHEUS_DIR}/server.key"
CERT_PATH = f"{PROMETHEUS_DIR}/server.cert"
WEB_CONFIG_PATH = f"{PROMETHEUS_DIR}/prometheus-web-config.yml"

# To get the behaviour consistent with mimir that doesn't allow lower values
# than 100k exemplars, we set the same floor in prometheus. If the user specifies
# a lower but positive value, we configure Prometheus to store 100k exemplars.
EXEMPLARS_FLOOR = 100000

logger = logging.getLogger(__name__)


def to_tuple(status: StatusBase) -> Tuple[str, str]:
    """Convert a StatusBase to tuple, so it is marshallable into StoredState."""
    return status.name, status.message


_STATUS_ACTIVE = to_tuple(ActiveStatus())


def blocked_status(message: str) -> Tuple[str, str]:
    return to_tuple(BlockedStatus(message))


class CompositeStatus(TypedDict):
    """Per-component status holder."""

    # These are going to go into stored state, so we must use marshallable objects.
    # They are passed to StatusBase.from_name().
    retention_size: Tuple[str, str]
    config: Tuple[str, str]


@dataclass
class TLSConfig:
    """TLS configuration received by the charm over the `certificates` relation."""

    server_cert: str
    ca_cert: str
    private_key: str


class PrometheusCharm(CharmBase):
    """A Juju Charm for Prometheus."""

    _stored = StoredState()

    def __init__(self, *args):
        super().__init__(*args)
        self._fqdn = socket.getfqdn()
        # Prometheus has a mix of pull and push statuses. We need stored state for push statuses.
        # https://discourse.charmhub.io/t/its-probably-ok-for-a-unit-to-go-into-error-state/13022
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
            # the `common_name` field is required but limited to 64 characters.
            # since it's overridden by sans, we can use a short,
            # constrained value like app name.
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
        """Pull file from container (without raising pebble errors).

        Returns:
            File contents if exists; None otherwise.
        """
        try:
            return cast(str, self.container.pull(path, encoding="utf-8").read())
        except (FileNotFoundError, PebbleError):
            # Drop FileNotFoundError https://github.com/canonical/operator/issues/896
            return None

    def _push(self, path, contents):
        """Push file to container, creating subdirs as necessary."""
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
        """Convert a string representation of percentage of 0-100%, to a 0-1 ratio.

        Raises:
            ValueError, if the percentage string is invalid or not within range.
        """
        if not percentage.endswith("%"):
            raise ValueError("Must be a number followed by '%', e.g. '80%'")
        value = float(percentage[:-1]) / 100.0
        if value < 0 or value > 1:
            raise ValueError("Percentage value must be in the range 0-100.")
        return value

    def _get_pvc_capacity(self) -> str:
        """Get PVC capacity from pod name.

        This may need to be handled differently once Juju supports multiple storage instances
        for k8s (https://bugs.launchpad.net/juju/+bug/1977775).
        """
        # Assuming the storage name is "databases" (must match metadata.yaml).
        # This assertion would be picked up by every integration test so no concern this would
        # reach production.
        assert "database" in self.model.storages, (
            "The 'database' storage is no longer in metadata: must update literals in charm code."
        )

        # Get PVC capacity from kubernetes
        client = Client()  # pyright: ignore
        pod_name = self.unit.name.replace("/", "-", -1)

        # Take the first volume whose name starts with "<app-name>-database-".
        # The volumes array looks as follows for app "am" and storage "data":
        # 'volumes': [{'name': 'am-data-d7f6a623',
        #              'persistentVolumeClaim': {'claimName': 'am-data-d7f6a623-am-0'}}, ...]
        pvc_name = ""
        for volume in cast(
            Pod, client.get(Pod, name=pod_name, namespace=self.model.name)
        ).spec.volumes:  # pyright: ignore
            if not volume.persistentVolumeClaim:
                # The volumes 'charm-data' and 'kube-api-access-xxxxx' do not have PVCs - filter
                # those out.
                continue
            # claimName looks like this: 'prom-database-325a0ee8-prom-0'
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

        # The other kind of storage to query for is
        # client.get(...).spec.resources.requests["storage"]
        # but to ensure prometheus does not fill up storage we need to limit the actual value
        # (status.capacity) and not the requested value (spec.resources.requests).

        return capacity

    def _generate_command(self) -> str:
        """Construct command to launch Prometheus.

        Returns:
            a string consisting of Prometheus command and associated
            command line options.
        """
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

        # For stripPrefix middleware to work correctly, we need to set web.external-url and
        # web.route-prefix in a particular way.
        # https://github.com/prometheus/prometheus/issues/1191
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
            # `storage.tsdb.retention.size` uses the legacy binary format, so "GB" and not "GiB"
            # https://github.com/prometheus/prometheus/issues/10768
            # For simplicity, always communicate to prometheus in GiB
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
        """Returns workload's FQDN. Used for ingress."""
        scheme = "https" if self._tls_available else "http"
        return f"{scheme}://{self._fqdn}:{self._port}"

    @property
    def external_url(self) -> Optional[str]:
        """Return the external hostname received from an ingress relation, if it exists."""
        try:
            if ingress_url := self.ingress.url:
                return ingress_url
        except ModelError as e:
            logger.error("Failed obtaining external url: %s. Shutting down?", e)
        return None

    @property
    def most_external_url(self) -> str:
        """Return the most external url known about by this charm.

        This will return the first of:
        - the external URL, if the ingress is configured and ready
        - the internal URL
        """
        external_url = self.external_url
        if external_url:
            return external_url

        return self.internal_url

    @property
    def log_level(self):
        """The log level configured for the charm."""
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

    # TLS CONFIG
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
        """Return the web.config.file contents as a dict, if TLS is enabled; otherwise None.

        Ref: https://prometheus.io/docs/prometheus/latest/configuration/https/
        """
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
