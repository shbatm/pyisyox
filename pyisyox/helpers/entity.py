"""An abstract class for entities."""
from __future__ import annotations

from abc import ABC
from dataclasses import asdict
from datetime import datetime
import json
from typing import TYPE_CHECKING, Generic, TypeVar

from pyisyox.constants import Protocol
from pyisyox.helpers.events import EventEmitter
from pyisyox.helpers.models import EntityDetailT, EntityStatus, StatusT

# Typing imports that create a circular dependency
if TYPE_CHECKING:
    from pyisyox.helpers.entity_platform import EntityPlatform
    from pyisyox.isy import ISY


EntityT = TypeVar("EntityT", bound="Entity")


class Entity(ABC, Generic[EntityDetailT, StatusT]):
    """An abstract class for ISY entities.

    For consistency with downstream users of this module, every
    class for the different platforms inherits some essential properties
    from this class.

    Note: Adapted from home-assistant/core.
    """

    # SAFE TO OVERWRITE
    # The properties and methods here are safe to overwrite when inheriting
    # this class. These may be used to customize the behavior of the entity.
    _address: str = None  # type: ignore[assignment]

    # Protocol must be set in inheriting class
    _protocol: Protocol = None  # type: ignore[assignment]

    # Owning isy instance. Will be set by platform
    # While not purely typed, it makes typehinting more useful for us
    # and removes the need for constant None checks or asserts.
    isy: ISY = None  # type: ignore[assignment]

    # Owning platform instance. Will be set by EntityPlatform
    platform: EntityPlatform | None = None

    _enabled: bool = True
    _last_changed: datetime
    _last_update: datetime
    _status: StatusT
    _name: str = ""

    detail: EntityDetailT
    status_events: EventEmitter

    @property
    def address(self) -> str:
        """Return the entity ID."""
        return self._address

    @property
    def enabled(self) -> bool:
        """Return if the entity is enabled on the controller."""
        return self._enabled

    @property
    def last_changed(self) -> datetime:
        """Return the UTC Time of the last status change for this entity."""
        return self._last_changed

    @property
    def last_update(self) -> datetime:
        """Return the UTC Time of the last update for this entity."""
        return self._last_update

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._name

    @property
    def protocol(self) -> Protocol:
        """Return the protocol for this entity."""
        return self._protocol

    @property
    def status(self) -> StatusT:
        """Return the current entity state."""
        return self._status

    def update_enabled(self, value: bool) -> None:
        """Set if the entity is enabled on the controller."""
        if self._enabled != value:
            self._enabled = value
            self.update_status(self.status, force=True)

    def update_entity(self, name: str, detail: EntityDetailT) -> None:
        """Update an entity information."""
        _changed = False
        if name != self.name:
            self._name = name
            _changed = False
        if detail != self.detail:
            self.detail = detail
            _changed = False
        if _changed:
            self.update_status(self.status, force=True)

    def update_status(self, value: StatusT, force: bool = False) -> None:
        """Set the current entity state and notify listeners."""
        self._last_update = datetime.now()
        if self._status != value:
            self._status = value
            force = True

        if not force:
            return

        self._last_changed = datetime.now()
        status = EntityStatus(
            self.address,
            self.status,
            self.detail,
            self._last_changed,
            self._last_update,
        )
        self.status_events.notify(status)

        # Also notify platform-level subscribers
        if self.platform is not None:
            self.platform.status_events.notify(status)

    def update_last_changed(self, timestamp: datetime | None = None) -> None:
        """Set the UTC Time of the last status change for this entity."""
        if timestamp is None:
            timestamp = datetime.now()
        self._last_changed = timestamp

    def update_last_update(self, timestamp: datetime | None = None) -> None:
        """Set the UTC Time of the last update for this entity."""
        if timestamp is None:
            timestamp = datetime.now()
        self._last_update = timestamp

    def __str__(self) -> str:
        """Return a string representation of the entity."""
        return f"{self.name} ({self.address})"

    def __repr__(self) -> str:
        """Return a string representation of the entity."""
        return (
            f"{type(self).__name__}(name='{self.name}' address='{self.address}')"
            f" detail:\n{json.dumps(asdict(self.detail), indent=4, sort_keys=True, default=str)}"
        )
