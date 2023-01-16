"""Base object for nodes and groups."""
from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from xml.dom import minidom

from pyisy.constants import (
    CMD_BEEP,
    CMD_BRIGHTEN,
    CMD_DIM,
    CMD_DISABLE,
    CMD_ENABLE,
    CMD_FADE_DOWN,
    CMD_FADE_STOP,
    CMD_FADE_UP,
    CMD_OFF,
    CMD_OFF_FAST,
    CMD_ON,
    CMD_ON_FAST,
    COMMAND_FRIENDLY_NAME,
    METHOD_COMMAND,
    NODE_FAMILY_ID,
    TAG_DESCRIPTION,
    TAG_IS_LOAD,
    TAG_LOCATION,
    TAG_NAME,
    TAG_SPOKEN,
    URL_CHANGE,
    URL_NODES,
    URL_NOTES,
    XML_TRUE,
)
from pyisy.entity import Entity, EntityStatus
from pyisy.exceptions import XML_ERRORS, XML_PARSE_ERROR, ISYResponseParseError
from pyisy.helpers import NodeNotes, NodeProperty, now, value_from_xml
from pyisy.logging import _LOGGER

if TYPE_CHECKING:
    from pyisy.nodes import Nodes


class NodeBase(Entity):
    """Base Object for Nodes and Groups/Scenes."""

    has_children = False
    _aux_properties: dict[str, NodeProperty]
    _family: str
    _id: str
    _name: str
    _nodes: Nodes
    _notes: NodeNotes | None
    _primary_node: str
    _flag: int
    _status: int | float
    _last_update: datetime
    _last_changed: datetime

    def __init__(
        self,
        nodes: Nodes,
        address: str,
        name: str,
        status: int | float,
        family_id: str = "",
        aux_properties: dict[str, NodeProperty] | None = None,
        pnode: str = "",
        flag: int = 0,
    ):
        """Initialize a Node Base class."""
        self._aux_properties = aux_properties if aux_properties is not None else {}
        self._family = NODE_FAMILY_ID.get(family_id, family_id)
        self._address = address
        self._name = name
        self._nodes = nodes
        self._notes = None
        self._primary_node = pnode
        self._flag = flag
        self._status = status
        self._last_update = now()
        self._last_changed = now()
        self.isy = nodes.isy

    def __str__(self) -> str:
        """Return a string representation of the node."""
        return f"{type(self).__name__}({self._id})"

    @property
    def aux_properties(self) -> dict[str, NodeProperty]:
        """Return the aux properties that were in the Node Definition."""
        return self._aux_properties

    @property
    def description(self) -> str:
        """Return the description of the node from it's notes."""
        if self._notes is None:
            _LOGGER.debug(
                "No notes retrieved for node. Call get_notes() before accessing."
            )
            return ""
        return self._notes.description

    @property
    def family(self) -> str:
        """Return the ISY Family category."""
        return self._family

    @property
    def flag(self) -> int:
        """Return the flag of the current node as a property."""
        return self._flag

    @property
    def folder(self) -> str:
        """Return the folder of the current node as a property."""
        return self._nodes.get_folder(self.address)

    @property
    def is_load(self) -> bool:
        """Return the isLoad property of the node from it's notes."""
        if self._notes is None:
            _LOGGER.debug(
                "No notes retrieved for node. Call get_notes() before accessing."
            )
            return False
        return self._notes.is_load

    @property
    def location(self) -> str:
        """Return the location of the node from it's notes."""
        if self._notes is None:
            _LOGGER.debug(
                "No notes retrieved for node. Call get_notes() before accessing."
            )
            return ""
        return self._notes.location

    @property
    def primary_node(self) -> str:
        """Return just the parent/primary node address.

        This is similar to Node.parent_node but does not return the whole Node
        class, and will return itself if it is the primary node/group.

        """
        return self._primary_node

    @property
    def spoken(self) -> str:
        """Return the text of the Spoken property inside the group notes."""
        if self._notes is None:
            _LOGGER.debug(
                "No notes retrieved for node. Call get_notes() before accessing."
            )
            return ""
        return self._notes.spoken

    async def get_notes(self) -> None:
        """Retrieve and parse the notes for a given node.

        Notes are not retrieved unless explicitly requested by
        a call to this function.
        """
        notes_xml = await self.isy.conn.request(
            self.isy.conn.compile_url([URL_NODES, self._id, URL_NOTES]), ok404=True
        )
        if notes_xml is not None and notes_xml != "":
            try:
                notes_dom = minidom.parseString(notes_xml)
            except XML_ERRORS as exc:
                _LOGGER.error("%s: Node Notes %s", XML_PARSE_ERROR, notes_xml)
                raise ISYResponseParseError() from exc

            spoken = value_from_xml(notes_dom, TAG_SPOKEN)
            location = value_from_xml(notes_dom, TAG_LOCATION)
            description = value_from_xml(notes_dom, TAG_DESCRIPTION)
            is_load = value_from_xml(notes_dom, TAG_IS_LOAD)
        self._notes = NodeNotes(
            spoken=spoken,
            is_load=is_load == XML_TRUE,
            description=description,
            location=location,
        )

    async def update(
        self,
        event: NodeProperty | None = None,
        wait_time: float = 0,
        xmldoc: minidom.Element | None = None,
    ) -> None:
        """Update the group with values from the controller."""
        self.update_last_update()

    def update_property(self, prop: NodeProperty) -> None:
        """Update an aux property for the node when received."""
        self.update_last_update()

        aux_prop = self.aux_properties.get(prop.control)
        if aux_prop:
            if prop.uom == "" and not aux_prop.uom == "":
                # Guard against overwriting known UOM with blank UOM (ISYv4).
                prop.uom = aux_prop.uom
            if aux_prop == prop:
                return
        self.aux_properties[prop.control] = prop
        self.update_last_changed()
        self.status_events.notify(
            EntityStatus(
                self.address, self.status, self._last_changed, self._last_update
            )
        )

    async def send_cmd(
        self,
        cmd: str,
        val: str | int | float | None = None,
        uom: str | None = None,
        query: dict[str, str] | None = None,
    ) -> bool:
        """Send a command to the device."""
        value = str(val) if val is not None else None
        _uom = str(uom) if uom is not None else None
        req = [URL_NODES, str(self._id), METHOD_COMMAND, cmd]
        if value:
            req.append(value)
        if _uom:
            req.append(_uom)
        req_url = self.isy.conn.compile_url(req, query)
        if not await self.isy.conn.request(req_url):
            _LOGGER.warning(
                "ISY could not send %s command to %s.",
                COMMAND_FRIENDLY_NAME.get(cmd),
                self._id,
            )
            return False
        _LOGGER.debug(
            "ISY command %s sent to %s.", COMMAND_FRIENDLY_NAME.get(cmd), self._id
        )
        return True

    async def beep(self) -> bool:
        """Identify physical device by sound (if supported)."""
        return await self.send_cmd(CMD_BEEP)

    async def brighten(self) -> bool:
        """Increase brightness of a device by ~3%."""
        return await self.send_cmd(CMD_BRIGHTEN)

    async def dim(self) -> bool:
        """Decrease brightness of a device by ~3%."""
        return await self.send_cmd(CMD_DIM)

    async def disable(self) -> bool:
        """Send command to the node to disable it."""
        if not await self.isy.conn.request(
            self.isy.conn.compile_url([URL_NODES, str(self._id), CMD_DISABLE])
        ):
            _LOGGER.warning("ISY could not %s %s.", CMD_DISABLE, self._id)
            return False
        return True

    async def enable(self) -> bool:
        """Send command to the node to enable it."""
        if not await self.isy.conn.request(
            self.isy.conn.compile_url([URL_NODES, str(self._id), CMD_ENABLE])
        ):
            _LOGGER.warning("ISY could not %s %s.", CMD_ENABLE, self._id)
            return False
        return True

    async def fade_down(self) -> bool:
        """Begin fading down (dim) a device."""
        return await self.send_cmd(CMD_FADE_DOWN)

    async def fade_stop(self) -> bool:
        """Stop fading a device."""
        return await self.send_cmd(CMD_FADE_STOP)

    async def fade_up(self) -> bool:
        """Begin fading up (dim) a device."""
        return await self.send_cmd(CMD_FADE_UP)

    async def fast_off(self) -> bool:
        """Start manually brightening a device."""
        return await self.send_cmd(CMD_OFF_FAST)

    async def fast_on(self) -> bool:
        """Start manually brightening a device."""
        return await self.send_cmd(CMD_ON_FAST)

    async def query(self) -> bool:
        """Request the ISY query this node."""
        return await self.isy.query(address=self.address)

    async def turn_off(self) -> bool:
        """Turn off the nodes/group in the ISY."""
        return await self.send_cmd(CMD_OFF)

    async def turn_on(self, val: int | str | None = None) -> bool:
        """
        Turn the node on.

        |  [optional] val: The value brightness value (0-255) for the node.
        """
        if val is None or type(self).__name__ == "Group":
            cmd = CMD_ON
        elif int(val) > 0:
            cmd = CMD_ON
            val = str(val) if int(val) <= 255 else None
        else:
            cmd = CMD_OFF
            val = None
        return await self.send_cmd(cmd, val)

    async def rename(self, new_name: str) -> bool:
        """
        Rename the node or group in the ISY.

        Note: Feature was added in ISY v5.2.0, this will fail on earlier versions.
        """
        # /rest/nodes/<nodeAddress>/change?name=<newName>
        req_url = self.isy.conn.compile_url(
            [URL_NODES, self._id, URL_CHANGE],
            query={TAG_NAME: new_name},
        )
        if not await self.isy.conn.request(req_url):
            _LOGGER.warning(
                "ISY could not update name for %s.",
                self._id,
            )
            return False
        _LOGGER.debug("ISY renamed %s to %s.", self._id, new_name)

        self._name = new_name
        return True
