from __future__ import annotations

import hashlib
from functools import partial
from typing import Any, FrozenSet, Mapping, Sequence, assert_never

import yaml
from charms.prometheus_k8s.v0.prometheus_scrape import PrometheusConfig
from cosl.time_validation import is_valid_timespec
from error_types import *
from input_types import *
from ops.pebble import Layer
from output_types import *
from step2_functional.charm import PROMETHEUS_DIR

from result import *

type WithErrors[A] = Result[Sequence[ConfigError], A]


def compute(input: ConfigInput) -> Result[ConfigErrors, ConfigOutcome]:
    match input:
        case ConnectedInput():
            outcome = compute_connected(input)
        case DisconnectedInput():
            outcome = Ok(ContainerOffline())
        case _ as unreachable:
            assert_never(unreachable)

    return outcome


def compute_connected(input: ConnectedInput) -> Result[ConfigErrors, ConfigOutcome]:
    prometheus_status = compute_prometheus_status(input)
    layer_status = compute_layer_status(input)

    return prometheus_status.map(partial(compute_final_outcome, layer_status))


def compute_global(input: ConnectedInput) -> WithErrors[Mapping[str, Any]]:
    def on_error(interval: str):
        return [ConfigError(f"Invalid evaluation_interval: [{interval}]")]

    def build_global_config(evaluation_interval: str):
        return {
            "global": {
                "scrape_interval": "1m",
                "scrape_timeout": "10s",
                "evaluation_interval": evaluation_interval,
            }
        }

    return Result.validate(
        input.config.evaluation_interval,
        is_valid_timespec,
        on_error,
    ).map(build_global_config)


def compute_alerting(
    input: ConnectedInput,
) -> WithErrors[Mapping[str, Any]]:
    scheme = input.config.alertmanager_scheme

    def non_empty[A](set: FrozenSet[A]):
        return bool(set)

    def on_error(_):
        return [ConfigError("No alertmanagers available")]

    def build_from_non_empty(alert_managers: FrozenSet[str]):
        alerting = PrometheusConfig.render_alertmanager_static_configs(list(alert_managers))
        return alerting | {
            "alertmanagers": [am | {"scheme": scheme} for am in alerting["alertmanagers"]]
        }

    return Result.validate(
        input.alert_managers,
        non_empty,
        on_error,
    ).map(build_from_non_empty)


def compute_scrape_jobs(
    input: ConnectedInput,
) -> WithErrors[Sequence[Mapping[str, Any]]]:
    def with_honor(job: Mapping[str, Any]):
        return {**job, "honor_labels": input.config.honor_labels}

    honored_jobs = list(map(with_honor, input.scrape_jobs))

    return Result.traverse(honored_jobs, process_tls_config)


def compute_prometheus_status(
    input: ConnectedInput,
) -> Result[ConfigErrors, PrometheusReloadStatus]:
    return (
        Result.combine3(
            compute_global(input),
            compute_alerting(input),
            compute_scrape_jobs(input),
        )
        .using(combine_configs)
        .map(partial(compute_reload_status, input.old_config_hash))
        .map_error(combine_errors)
    )


def compute_reload_status(old_config_hash: str, config: Mapping):
    config_hash = sha256(yaml.safe_dump(config))
    same_config = old_config_hash == config_hash

    full_conf = PrometheusConf(config, config_hash)

    return NoReload() if same_config else ReloadConf(full_conf)


def compute_layer_status(input: ConnectedInput) -> LayerRestartStatus:
    new_layer = prometheus_layer(name=input.charm_name, command=input.command)
    all_svcs_running = all(svc.is_running() for svc in input.current_services.values())
    services_unchanged = input.current_planned_services == new_layer.services
    no_restart = services_unchanged and all_svcs_running

    return NoRestart() if no_restart else RestartLayer(new_layer)


def compute_final_outcome(
    layer_status: LayerRestartStatus,
    prometheus_status: PrometheusReloadStatus,
) -> ConfigOutcome:
    match (prometheus_status, layer_status):
        case (NoReload(), NoRestart()):
            outcome = Noop()
        case (NoReload(), RestartLayer(layer)):
            outcome = RestartOnly(layer)
        case (ReloadConf(prometheus_config), NoRestart()):
            outcome = PushReload(prometheus_config)
        case (ReloadConf(prometheus_config), RestartLayer(layer)):
            # a restart makes the reload redundant
            outcome = PushRestart(prometheus_config, layer)
        case _ as unreachable:
            assert_never(unreachable)

    return outcome


def prometheus_layer(name: str, command: str) -> Layer:
    return Layer(
        {
            "summary": "Prometheus layer",
            "description": "Pebble layer configuration for Prometheus",
            "services": {
                name: {
                    "override": "replace",
                    "summary": "prometheus daemon",
                    "command": command,
                    "startup": "enabled",
                }
            },
        }
    )


def process_tls_config(job: Mapping[str, Any]) -> WithErrors[Mapping[str, Any]]:
    def with_dir_prefix(name: str) -> str:
        return f"{PROMETHEUS_DIR}/{job['job_name']}-{name}"

    def get_tls_overrides(tls_config: Mapping[str, Any]):
        cert_file = tls_config.get("cert_file")
        key_file = tls_config.get("key_file")

        if cert_file and key_file:
            result = Ok(
                {
                    "cert_file": with_dir_prefix("client.crt"),
                    "cert_file_content": cert_file,
                    "key_file": with_dir_prefix("client.key"),
                    "key_file_content": key_file,
                }
            )
        elif cert_file or key_file:  # only one of the file present
            result = Err([tls_files_error()])
        else:
            result = Ok({})

        return result

    def apply_tls_overrides(tls_config: Mapping[str, Any], overrides: Mapping[str, Any]):
        return {**job, "tls_config": {**tls_config, **overrides}} if overrides else job

    tls_config = job.get("tls_config", {})

    if tls_config:
        result = get_tls_overrides(tls_config).map(partial(apply_tls_overrides, tls_config))
    else:
        result = Ok(job)

    return result


def tls_files_error():
    return ConfigError(
        'tls_config requires both "cert_file" and "key_file" if client '
        "authentication is to be used"
    )


def combine_configs(
    global_config: Mapping[str, Any],
    alerting: Mapping[str, Any],
    scrape: Sequence[Mapping[str, Any]],
):
    return {
        "global": global_config,
        "alerting": alerting,
        "scrape_configs": scrape,
    }


def combine_errors(errors: Sequence[ConfigError]) -> ConfigErrors:
    return ConfigErrors("config validation failed", errors)


def sha256(hashable) -> str:
    if isinstance(hashable, str):
        hashable = hashable.encode("utf-8")
    return hashlib.sha256(hashable).hexdigest()
