#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.
#
# Learn more at: https://juju.is/docs/sdk

import logging
from types import TracebackType
from typing import Any, Dict, Optional

from ops import Unit
from ops.charm import ActionEvent, CharmBase
from ops.model import ModelError, SecretNotFoundError
from ops.pebble import ServiceInfo
from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)
from typing_extensions import Self, Type

logger = logging.getLogger(__name__)


class KratosCharm(CharmBase):
    def __init__(self, *args: Any) -> None:
        super().__init__(*args)

        self._workload_service = WorkloadService(self.unit)

        self.framework.observe(self.on.reset_password_action, self._on_reset_password_action)

    def _on_reset_password_action(self, event: ActionEvent) -> None:
        if not self._workload_service.is_running():
            event.fail("Service is not ready. Please re-run the action when the charm is active")
            return

        password = None
        password_secret_id = event.params.get("password-secret-id")
        if password_secret_id:
            try:
                juju_secret = self.model.get_secret(id=password_secret_id)
                password = juju_secret.get_content().get("password")
            except SecretNotFoundError:
                event.fail("Juju secret not found")
                return
            except ModelError as err:
                event.fail(f"An error occurred when fetching the juju secret: {err}")
                return

        with HTTPClient(base_url="http://127.0.0.1:9990") as client:
            try:
                identity_params = IdentityParams.model_validate(
                    event.params, context={"http_client": client}
                )
            except ValueError as e:
                event.fail(f"{e}")
                return

            identity_id = identity_params.identity_id
            assert identity_id is not None
            try:
                if password:
                    res = Identity(client).reset_password(identity_id, password)
                else:
                    res = client.create_recovery_code(identity_id)
            except IdentityNotExistsError:
                event.fail(f"Identity {identity_id} does not exist")
                return
            except ClientRequestError:
                event.fail("Failed to request Kratos API")
                return

        if password:
            event.log("Password was changed successfully")
        else:
            event.log(
                "Recovery code created successfully. Use the returned link to reset the identity's password"
            )

        event.set_results(dict_to_action_output(res))


# ---------------------------------------------------------------------------
# exceptions
# ---------------------------------------------------------------------------


class CharmError(Exception): ...


class ActionError(CharmError): ...


class TooManyIdentitiesError(ActionError): ...


class IdentityNotExistsError(ActionError): ...


class ClientRequestError(ActionError): ...


# ---------------------------------------------------------------------------
# services
# ---------------------------------------------------------------------------


class WorkloadService:
    def __init__(self, unit: Unit) -> None: ...
    def get_service(self) -> Optional[ServiceInfo]: ...
    def is_running(self) -> bool: ...


# ---------------------------------------------------------------------------
# clients
# ---------------------------------------------------------------------------


class HTTPClient:
    def __init__(self, base_url: str) -> None: ...
    def __enter__(self) -> Self: ...
    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: TracebackType,
    ) -> None: ...
    def get_identity(self, identity_id: str, *, params: Optional[dict] = None) -> dict: ...
    def get_identity_by_email(self, email: str) -> dict: ...
    def reset_password(self, identity: dict, password: str) -> dict: ...
    def create_recovery_code(self, identity_id: str, expires_in: str = "1h") -> dict: ...


class Identity:
    def __init__(self, client: HTTPClient) -> None: ...
    def get(self, identity_id: str) -> dict: ...
    def reset_password(self, identity_id: str, password: str) -> dict: ...


# ---------------------------------------------------------------------------
# utils
# ---------------------------------------------------------------------------


def dict_to_action_output(d: Dict) -> Dict:
    """Convert all keys in a dict to the format of a juju action output.

    Recursively replaces underscores in dict keys with dashes.

    For example:
        {"a_b_c": 123} -> {"a-b-c": 123}
        {"a_b": {"c_d": "aba"}} -> {"a-b": {"c-d": "aba"}}

    """
    return {
        k.replace("_", "-"): dict_to_action_output(v) if isinstance(v, dict) else v
        for k, v in d.items()
    }


# ---------------------------------------------------------------------------
# actions
# ---------------------------------------------------------------------------


class IdentityParams(BaseModel):
    identity_id: Optional[str] = Field(default=None, alias="identity-id")
    email: Optional[EmailStr] = Field(default=None)

    model_config = ConfigDict(
        validate_by_name=True,
        validate_by_alias=True,
        extra="ignore",
    )

    @field_validator("identity_id", mode="before")
    @classmethod
    def validate_identity_id(cls, v: Optional[str]) -> Optional[str]: ...

    @model_validator(mode="after")
    def populate_identity_id(self, info: ValidationInfo) -> Self: ...
