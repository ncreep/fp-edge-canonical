# Copyright 2022 Canonical Ltd.
# See LICENSE file for licensing details.

r"""# Interface Library for ingress_per_unit.

This library wraps relation endpoints using the `ingress_per_unit` interface
and provides a Python API for both requesting and providing per-unit
ingress.

## Getting Started

To get started using the library, you just need to fetch the library using `charmcraft`.

```shell
charmcraft fetch-lib charms.traefik_k8s.v1.ingress_per_unit
```

Add the `jsonschema` dependency to the `requirements.txt` of your charm.

```yaml
requires:
    ingress:
        interface: ingress_per_unit
        limit: 1
```

Then, to initialise the library:

```python
from charms.traefik_k8s.v1.ingress_per_unit import (IngressPerUnitRequirer,
  IngressPerUnitReadyForUnitEvent, IngressPerUnitRevokedForUnitEvent)

class SomeCharm(CharmBase):
  def __init__(self, *args):
    # ...
    self.ingress_per_unit = IngressPerUnitRequirer(self, port=80)
    # The following event is triggered when the ingress URL to be used
    # by this unit of `SomeCharm` is ready (or changes).
    self.framework.observe(
        self.ingress_per_unit.on.ready_for_unit, self._on_ingress_ready
    )
    self.framework.observe(
        self.ingress_per_unit.on.revoked_for_unit, self._on_ingress_revoked
    )

    def _on_ingress_ready(self, event: IngressPerUnitReadyForUnitEvent):
        # event.url is the same as self.ingress_per_unit.url
        logger.info("This unit's ingress URL: %s", event.url)

    def _on_ingress_revoked(self, event: IngressPerUnitRevokedForUnitEvent):
        logger.info("This unit no longer has ingress")
```

If you wish to be notified also (or instead) when another unit's ingress changes
(e.g. if you're the leader and you're doing things with your peers' ingress),
you can pass `listen_to = "all-units" | "both"` to `IngressPerUnitRequirer`
and observe `self.ingress_per_unit.on.ready` and `self.ingress_per_unit.on.revoked`.
"""

import logging
import socket
import typing
from typing import Any, Dict, List, Optional, Tuple

import yaml
from ops import EventBase
from ops.charm import CharmBase, RelationEvent
from ops.framework import EventSource, Object, ObjectEvents, StoredState
from ops.model import Application, ModelError, Relation, Unit

# The unique Charmhub library identifier, never change it
LIBID = "7ef06111da2945ed84f4f5d4eb5b353a"

# Increment this major API version when introducing breaking changes
LIBAPI = 1

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 22

log = logging.getLogger(__name__)

try:
    import jsonschema

    DO_VALIDATION = True
except ModuleNotFoundError:
    log.warning(
        "The `ingress_per_unit` library needs the `jsonschema` package to be able "
        "to do runtime data validation; without it, it will still work but validation "
        "will be disabled. \n"
        "It is recommended to add `jsonschema` to the 'requirements.txt' of your charm, "
        "which will enable this feature."
    )
    DO_VALIDATION = False

# LIBRARY GLOBS
RELATION_INTERFACE = "ingress_per_unit"
DEFAULT_RELATION_NAME = RELATION_INTERFACE.replace("_", "-")

INGRESS_REQUIRES_UNIT_SCHEMA = {
    "type": "object",
    "properties": {
        "model": {"type": "string"},
        "name": {"type": "string"},
        "host": {"type": "string"},
        "port": {"type": "string"},
        "mode": {"type": "string"},
        "strip-prefix": {"type": "string"},
        "redirect-https": {"type": "string"},
        "scheme": {"type": "string"},
    },
    "required": ["model", "name", "host", "port"],
}
INGRESS_PROVIDES_APP_SCHEMA = {
    "type": "object",
    "properties": {
        "ingress": {
            "type": "object",
            "patternProperties": {
                "": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string"},
                    },
                    "required": ["url"],
                }
            },
        }
    },
    "required": ["ingress"],
}

# TYPES
try:
    from typing import Literal, TypedDict  # type: ignore
except ImportError:
    from typing_extensions import Literal, TypedDict  # type: ignore  # py35 compat


# Model of the data a unit implementing the requirer will need to provide.
RequirerData = TypedDict(
    "RequirerData",
    {
        "model": str,
        "name": str,
        "host": str,
        "port": int,
        "mode": Optional[Literal["tcp", "http"]],
        "strip-prefix": Optional[bool],
        "redirect-https": Optional[bool],
        "scheme": Optional[Literal["http", "https"]],
    },
    total=False,
)


RequirerUnitData = Dict[Unit, "RequirerData"]
KeyValueMapping = Dict[str, str]
ProviderApplicationData = Dict[str, KeyValueMapping]




def _validate_data(data: Any, schema: Any) -> None:
    """Checks whether `data` matches `schema`.

    Will raise DataValidationError if the data is not valid, else return None.
    """
    if not DO_VALIDATION:
        return
    try:
        jsonschema.validate(instance=data, schema=schema)  # pyright: ignore[reportPossiblyUnboundVariable]
    except jsonschema.ValidationError as e:  # pyright: ignore[reportPossiblyUnboundVariable]
        raise DataValidationError(data, schema) from e


# EXCEPTIONS
class DataValidationError(RuntimeError):
    """Raised when data validation fails on IPU relation data."""








class _IngressPerUnitBase(Object):
    """Base class for IngressPerUnit interface classes."""

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """Constructor for _IngressPerUnitBase.

        Args:
            charm: The charm that is instantiating the instance.
            relation_name: The name of the relation name to bind to
                (defaults to "ingress-per-unit").
        """
        super().__init__(charm, relation_name)
        self.charm: CharmBase = charm

        self.relation_name = relation_name
        self.app = self.charm.app
        self.unit = self.charm.unit

        observe = self.framework.observe
        rel_events = charm.on[relation_name]
        observe(rel_events.relation_created, self._handle_relation)
        observe(rel_events.relation_joined, self._handle_relation)
        observe(rel_events.relation_changed, self._handle_relation)
        observe(rel_events.relation_departed, self._handle_relation)
        observe(rel_events.relation_broken, self._handle_relation_broken)
        observe(charm.on.leader_elected, self._handle_upgrade_or_leader)  # type: ignore
        observe(charm.on.upgrade_charm, self._handle_upgrade_or_leader)  # type: ignore

    @property
    def relations(self) -> List[Relation]:
        """The list of Relation instances associated with this relation_name."""
        return list(self.charm.model.relations[self.relation_name])

    def _handle_relation(self, event: RelationEvent) -> None:
        """Subclasses should implement this method to handle a relation update."""
        pass

    def _handle_relation_broken(self, event: RelationEvent) -> None:
        """Subclasses should implement this method to handle a relation breaking."""
        pass

    def _handle_upgrade_or_leader(self, event: EventBase) -> None:
        """Subclasses should implement this method to handle upgrades or leadership change."""
        pass

    def is_ready(self, relation: Optional[Relation] = None) -> bool:
        """Checks whether the given relation is ready.

        A relation is ready if the remote side has sent valid data.
        """
        if relation is None:
            return any(map(self.is_ready, self.relations))
        if relation.app is None:
            # No idea why, but this happened once.
            return False
        if not relation.app.name:  # type: ignore
            # Juju doesn't provide JUJU_REMOTE_APP during relation-broken
            # hooks. See https://github.com/canonical/operator/issues/693
            return False
        return True












class _IPUEvent(RelationEvent):
    __args__: Tuple[str, ...] = ()
    __optional_kwargs__: Dict[str, Any] = {}

    @classmethod
    def __attrs__(cls):  # type: ignore
        return cls.__args__ + tuple(cls.__optional_kwargs__.keys())

    def __init__(self, handle, relation, *args, **kwargs):  # type: ignore
        super().__init__(handle, relation, app=relation.app)

        if not len(self.__args__) == len(args):
            raise TypeError("expected {} args, got {}".format(len(self.__args__), len(args)))

        for attr, obj in zip(self.__args__, args):
            setattr(self, attr, obj)
        for attr, default in self.__optional_kwargs__.items():
            obj = kwargs.get(attr, default)
            setattr(self, attr, obj)

    def snapshot(self) -> Dict[str, Any]:
        dct = super().snapshot()
        for attr in self.__attrs__():
            obj = getattr(self, attr)
            try:
                dct[attr] = obj
            except ValueError as e:
                raise ValueError(
                    "cannot automagically serialize {}: "
                    "override this method and do it "
                    "manually.".format(obj)
                ) from e
        return dct

    def restore(self, snapshot: Any) -> None:
        super().restore(snapshot)
        for attr, obj in snapshot.items():
            setattr(self, attr, obj)


class IngressPerUnitReadyEvent(_IPUEvent):
    """Ingress is ready (or has changed) for some unit.

    Attrs:
        `unit_name`: name of the unit for which ingress has been
            provided/has changed.
        `url`: the (new) url for that unit.
    """

    __args__ = ("unit_name", "url")
    if typing.TYPE_CHECKING:
        unit_name = ""
        url = ""


class IngressPerUnitReadyForUnitEvent(_IPUEvent):
    """Ingress is ready (or has changed) for this unit.

    Is only fired on the unit(s) for which ingress has been provided or
    has changed.
    Attrs:
        `url`: the (new) url for this unit.
    """

    __args__ = ("url",)
    if typing.TYPE_CHECKING:
        url = ""


class IngressPerUnitRevokedEvent(_IPUEvent):
    """Ingress is revoked (or has changed) for some unit.

    Attrs:
        `unit_name`: the name of the unit whose ingress has been revoked.
            this could be "THIS" unit, or a peer.
    """

    __args__ = ("unit_name",)

    if typing.TYPE_CHECKING:
        unit_name = ""


class IngressPerUnitRevokedForUnitEvent(RelationEvent):
    """Ingress is revoked (or has changed) for this unit.

    Is only fired on the unit(s) for which ingress has changed.
    """


class IngressPerUnitRequirerEvents(ObjectEvents):
    """Container for IUP events."""

    ready = EventSource(IngressPerUnitReadyEvent)
    revoked = EventSource(IngressPerUnitRevokedEvent)
    ready_for_unit = EventSource(IngressPerUnitReadyForUnitEvent)
    revoked_for_unit = EventSource(IngressPerUnitRevokedForUnitEvent)


class IngressPerUnitRequirer(_IngressPerUnitBase):
    """Implementation of the requirer of ingress_per_unit."""

    on: IngressPerUnitRequirerEvents = IngressPerUnitRequirerEvents()  # pyright: ignore[reportIncompatibleMethodOverride]
    # used to prevent spurious urls to be sent out if the event we're currently
    # handling is a relation-broken one.
    _stored = StoredState()

    def __init__(
        self,
        charm: CharmBase,
        relation_name: str = DEFAULT_RELATION_NAME,
        *,
        host: Optional[str] = None,
        port: Optional[int] = None,
        mode: Literal["tcp", "http"] = "http",
        listen_to: Literal["only-this-unit", "all-units", "both"] = "only-this-unit",
        strip_prefix: bool = False,
        redirect_https: bool = False,
        # FIXME: now that `provide_ingress_requirements` takes a scheme, this arg can be changed to
        #  str type in v2.
        scheme: typing.Callable[[], str] = lambda: "http",
    ):
        """Constructor for IngressPerUnitRequirer.

        The request args can be used to specify the ingress properties when the
        instance is created. If any are set, at least `port` is required, and
        they will be sent to the ingress provider as soon as it is available.
        All request args must be given as keyword args.

        Args:
            charm: the charm that is instantiating the library.
            relation_name: the name of the relation name to bind to
                (defaults to "ingress-per-unit"; relation must be of interface
                type "ingress_per_unit" and have "limit: 1").
            host: Hostname to be used by the ingress provider to address the
                requirer unit; if unspecified, the FQDN of the unit will be
                used instead.
            port: port to be used by the ingress provider to address the
                    requirer unit.
            mode: mode to be used between "tcp" and "http".
            listen_to: Choose which events should be fired on this unit:
                "only-this-unit": this unit will only be notified when ingress
                  is ready/revoked for this unit.
                "all-units": this unit will be notified when ingress is
                  ready/revoked for any unit of this application, including
                  itself.
                "all": this unit will receive both event types (which means it
                  will be notified *twice* of changes to this unit's ingress!).
            strip_prefix: remove prefixes from the URL path.
            redirect_https: redirect incoming requests to HTTPS
            scheme: callable returning the scheme to use when constructing the ingress url.
        """
        super().__init__(charm, relation_name)
        self._stored.set_default(current_urls=None)  # type: ignore

        # if instantiated with a port, and we are related, then
        # we immediately publish our ingress data  to speed up the process.
        self._host = host
        self._port = port
        self._mode = mode
        self._strip_prefix = strip_prefix
        self._redirect_https = redirect_https
        self._get_scheme = scheme

        self.listen_to = listen_to

        self.framework.observe(
            self.charm.on[self.relation_name].relation_changed, self._handle_relation
        )
        self.framework.observe(
            self.charm.on[self.relation_name].relation_broken, self._handle_relation
        )

    def _handle_relation(self, event: RelationEvent) -> None:
        # we calculate the diff between the urls we were aware of
        # before and those we know now
        previous_urls = self._stored.current_urls or {}  # type: ignore

        # since ops 2.10, breaking relations won't show up in self.model.relations, so we're safe
        # in assuming all relations that are there are alive and well.
        current_urls = self._urls_from_relation_data
        self._stored.current_urls = current_urls  # type: ignore

        removed = previous_urls.keys() - current_urls.keys()  # type: ignore
        changed = {a for a in current_urls if current_urls[a] != previous_urls.get(a)}  # type: ignore  # noqa

        this_unit_name = self.unit.name
        # do not use self.relation in this context because if
        # the event is relation-broken, self.relation might be None
        relation = event.relation
        if self.listen_to in {"only-this-unit", "both"}:
            if this_unit_name in changed:
                self.on.ready_for_unit.emit(relation, current_urls[this_unit_name])  # type: ignore

            if this_unit_name in removed:
                self.on.revoked_for_unit.emit(relation=relation, app=relation.app)  # type: ignore

        if self.listen_to in {"all-units", "both"}:
            for unit_name in changed:
                self.on.ready.emit(relation, unit_name, current_urls[unit_name])  # type: ignore

            for unit_name in removed:
                self.on.revoked.emit(relation, unit_name)  # type: ignore

        self._publish_auto_data()

    def _handle_upgrade_or_leader(self, event: EventBase) -> None:
        self._publish_auto_data()

    def _publish_auto_data(self) -> None:
        if self._port:
            self.provide_ingress_requirements(host=self._host, port=self._port)

    @property
    def relation(self) -> Optional[Relation]:
        """The established Relation instance, or None if still unrelated."""
        return self.relations[0] if self.relations else None

    def is_ready(self) -> bool:  # type: ignore
        """Checks whether the given relation is ready.

        Or any relation if not specified.
        A given relation is ready if the remote side has sent valid data.
        """
        if not self.relation:
            return False
        if super().is_ready(self.relation) is False:
            return False
        return bool(self.url)

    def provide_ingress_requirements(
        self, *, scheme: Optional[str] = None, host: Optional[str] = None, port: int
    ) -> None:
        """Publishes the data that Traefik needs to provide ingress.

        Args:
            scheme: Scheme to be used; if unspecified, use the one used by __init__.
            host: Hostname to be used by the ingress provider to address the
             requirer unit; if unspecified, FQDN will be used instead
            port: the port of the service (required)
        """
        # This public method may be used at various points of the charm lifecycle, possibly when
        # the ingress relation is not yet there.
        # Abort if there is no relation (instead of requiring the caller to guard against it).
        if not self.relation:
            return

        if not host:
            host = socket.getfqdn()

        if not scheme:
            # If scheme was not provided, use the one given to the constructor.
            scheme = self._get_scheme()

        data = {
            "model": self.model.name,
            "name": self.unit.name,
            "host": host,
            "port": str(port),
            "mode": self._mode,
            "scheme": scheme,
        }

        if self._strip_prefix:
            data["strip-prefix"] = "true"

        if self._redirect_https:
            data["redirect-https"] = "true"

        _validate_data(data, INGRESS_REQUIRES_UNIT_SCHEMA)
        self.relation.data[self.unit].update(data)

    @property
    def _urls_from_relation_data(self) -> Dict[str, str]:
        """The full ingress URLs to reach every unit.

        May return an empty dict if the URLs aren't available yet.
        """
        relation = self.relation
        if not relation:
            return {}

        if not relation.app or not relation.app.name:  # type: ignore
            # FIXME Workaround for https://github.com/canonical/operator/issues/693
            # We must be in a relation_broken hook
            return {}
        assert isinstance(relation.app, Application)  # type guard

        try:
            raw = relation.data.get(relation.app, {}).get("ingress")  # type: ignore
        except ModelError as e:
            log.debug(
                "Error {} attempting to read remote app data; "
                "probably we are in a relation_departed hook".format(e)
            )
            return {}

        if not raw:
            # remote side didn't send yet
            return {}

        data = yaml.safe_load(raw)
        _validate_data({"ingress": data}, INGRESS_PROVIDES_APP_SCHEMA)

        return {unit_name: unit_data["url"] for unit_name, unit_data in data.items()}

    @property
    def urls(self) -> Dict[str, str]:
        """The full ingress URLs to reach every unit.

        May return an empty dict if the URLs aren't available yet.
        """
        current_urls = self._urls_from_relation_data
        return current_urls

    @property
    def url(self) -> Optional[str]:
        """The full ingress URL to reach the current unit.

        May return None if the URL isn't available yet.
        """
        urls = self.urls
        if not urls:
            return None
        return urls.get(self.charm.unit.name)
