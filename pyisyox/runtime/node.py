"""Runtime ``Node`` — :class:`NodeRecord` + :class:`NodeDef` + client.

The primary user-facing device handle. Exposes structural fields,
current properties (updated in place by the WS dispatcher), and
:meth:`Node.send_command` for editor-validated command dispatch.
Commands go through the legacy ``/rest/nodes/{addr}/cmd/...`` XML
surface — no ``/api/*`` equivalent has been observed.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING, Any
from xml.etree import ElementTree as ET

from pyisyox.client import NodeType
from pyisyox.constants import (
    CMD_BACKLIGHT,
    CMD_CLIMATE_FAN_SETTING,
    CMD_CLIMATE_MODE,
    CMD_MANUAL_DIM_BEGIN,
    CMD_MANUAL_DIM_STOP,
    CMD_SECURE,
    PROP_BATTERY_LEVEL,
    PROP_ON_LEVEL,
    PROP_RAMP_RATE,
    PROP_SETPOINT_COOL,
    PROP_SETPOINT_HEAT,
    PROP_STATUS,
    NodeFamily,
    NodeFlag,
    Protocol,
)
from pyisyox.exceptions import ISYResponseParseError
from pyisyox.runtime._commands import NodeCommandError, encode_command_params
from pyisyox.runtime._normalize import normalize_property_value

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
    from pyisyox.schema.editor import Editor
    from pyisyox.schema.nodedef import NodeDef
    from pyisyox.schema.profile import Profile

__all__ = ["Node", "NodeCommandError"]


#: Two IoX family ids carry Z-Wave devices: ``"4"`` is the legacy
#: attached Z-Wave radio, ``"12"`` is the Z-Matter (800-series / Matter)
#: radio. Both classify as :attr:`Protocol.ZWAVE`.
_ZWAVE_FAMILY_IDS = frozenset({NodeFamily.ZWAVE, NodeFamily.ZMATTER_ZWAVE})

#: IoX *core* (non-plugin) family ids — everything in ``family.xsd``
#: plus the Z-Matter extension and the folder family, minus
#: ``NODESERVER``. A node whose family id is ``NODESERVER`` or any
#: value outside this set is a PG3 plugin node (plugins report a slot
#: id here), so it classifies as :attr:`Protocol.NODE_SERVER`.
_CORE_FAMILY_IDS = frozenset(NodeFamily) - {NodeFamily.NODESERVER}


class Node:
    """User-facing handle around one node from a :class:`LoadResult`.

    Construct via :meth:`Node.from_record` rather than the bare
    constructor so the editor resolver and nodedef are wired
    automatically from the parsed :class:`Profile`.
    """

    __slots__ = ("_client", "_nodedef", "_profile", "_record")

    def __init__(
        self,
        record: NodeRecord,
        nodedef: NodeDef | None,
        profile: Profile,
        client: IoXClient,
    ) -> None:
        """Store the components needed for state reads and command sends."""
        self._record = record
        self._nodedef = nodedef
        self._profile = profile
        self._client = client

    @classmethod
    def from_record(cls, record: NodeRecord, profile: Profile, client: IoXClient) -> Node:
        """Resolve the nodedef for ``record`` and construct a Node."""
        nodedef = profile.find_nodedef(record.nodedef_id, record.family_id, record.instance_id)
        return cls(record=record, nodedef=nodedef, profile=profile, client=client)

    # --- introspection ------------------------------------------------

    @property
    def address(self) -> str:
        """Wire address — e.g. ``"3D 7D 87 1"`` or ``"n010_84dd4c2c24c3b7"``."""
        return self._record.address

    @property
    def name(self) -> str:
        """User-assigned label (set in eisy admin UI)."""
        return self._record.name

    @property
    def nodedef_id(self) -> str:
        """The nodedef id (e.g. ``"KeypadDimmer_ADV"``, ``"flume2"``)."""
        return self._record.nodedef_id

    @property
    def family_id(self) -> str:
        """Family id — ``"1"`` for native Insteon/Z-Wave, slot id for plugins."""
        return self._record.family_id

    @property
    def instance_id(self) -> str:
        """Instance id within the family."""
        return self._record.instance_id

    @property
    def type(self) -> str:
        """IoX type triple, e.g. ``"1.65.69.0"`` for KeypadLinc dimmer.

        Plugin nodes carry a placeholder (Flume reports ``"1.2.3.4"``);
        consumers should not rely on it for plugin classification —
        use :attr:`nodedef` instead.
        """
        return self._record.type

    @property
    def parent_address(self) -> str | None:
        """Tree-hierarchy parent (containing folder). ``None`` at root.

        Distinct from :attr:`primary_address`: ``<parent>`` is the folder,
        ``<pnode>`` is the device primary for multi-button hardware.
        """
        return self._record.parent_address

    @property
    def primary_address(self) -> str | None:
        """Device primary for sub-button nodes (from ``<pnode>``).

        Sub-buttons of multi-button devices (KeypadLinc, RemoteLinc,
        FanLinc) carry the primary's address. ``None`` for primaries —
        so ``primary_address is not None`` reads as "sub-node" and
        ``primary_address or address`` as the device-grouping address.
        """
        pnode = self._record.pnode
        if pnode is None or pnode == self._record.address:
            return None
        return pnode

    @property
    def enabled(self) -> bool:
        """Whether the eisy considers this node active."""
        return self._record.enabled

    def _editor_for_property(self, prop_id: str) -> Editor | None:
        """Resolve the editor governing ``prop_id`` on this node's nodedef.

        ``None`` when the nodedef is unresolved, doesn't define the
        property, or references an editor the profile doesn't carry.
        """
        if self._nodedef is None:
            return None
        slot = self._nodedef.properties.get(prop_id)
        if slot is None or not slot.editor_id:
            return None
        return self._profile.find_editor(slot.editor_id, self.family_id, self.instance_id)

    @property
    def properties(self) -> dict[str, NodePropertyValue]:
        """Live property values, keyed by property id (e.g. ``"ST"``).

        Each value is UOM-normalised to its nodedef editor's canonical
        unit — e.g. an Insteon dimmer reporting ``OL`` as a UOM-100
        0-255 byte is surfaced as the UOM-51 0-100% the ``I_OL`` editor
        (and the ``/cmd`` write surface) uses. Values already matching
        the editor pass through unchanged.
        """
        record_props = self._record.properties
        return {
            pid: normalize_property_value(npv, self._editor_for_property(pid))
            for pid, npv in record_props.items()
        }

    @property
    def status(self) -> NodePropertyValue | None:
        """Shortcut for :attr:`properties`\\ ``[PROP_STATUS]`` — the
        node's primary status reading (``"ST"``), UOM-normalised the
        same way :attr:`properties` is.

        Returns ``None`` when the node hasn't reported ST yet (common
        for write-only Insteon controllers and plugin nodes that don't
        advertise ST). Consumers that want a scalar should read
        ``node.status.value`` (a string) and parse it themselves;
        the property keeps the structured shape so callers can also
        reach ``.uom``, ``.formatted``, etc.
        """
        npv = self._record.properties.get(PROP_STATUS)
        if npv is None:
            return None
        return normalize_property_value(npv, self._editor_for_property(PROP_STATUS))

    @property
    def nodedef(self) -> NodeDef | None:
        """The resolved :class:`NodeDef`, or ``None`` if unresolved."""
        return self._nodedef

    @property
    def flag(self) -> int:
        """Raw node-flag bitfield from the controller's node table.

        Bit meanings live in :class:`pyisyox.constants.NodeFlag`
        (``NEW``, ``IN_ERR``, ``DEVICE_ROOT``, ...). Use
        :meth:`has_flag` for individual bit checks rather than reading
        this directly. Returns ``0`` when the controller didn't carry a
        value for this node — treat ``0`` as "no bits set" rather than
        "unknown".
        """
        return self._record.flag

    def has_flag(self, flag: NodeFlag) -> bool:
        """Return ``True`` if every bit in ``flag`` is set on this node.

        ``flag`` may be OR'd; combined values must have every bit set.
        """
        return (self._record.flag & int(flag)) == int(flag)

    # --- introspection (derived) --------------------------------------

    @property
    def protocol(self) -> Protocol:
        """Transport-protocol classification from ``family_id``.

        Returns ``NODE_SERVER`` for any non-core family id (PG3 plugin
        nodes report a slot id here), ``UNKNOWN`` for recognised but
        unmapped core families. Classifies transport, not device class
        — use :attr:`is_thermostat` etc. for capability.
        """
        fid = self.family_id
        if fid == NodeFamily.INSTEON:
            return Protocol.INSTEON
        if fid == NodeFamily.UPB:
            return Protocol.UPB
        if fid in _ZWAVE_FAMILY_IDS:
            return Protocol.ZWAVE
        if fid == NodeFamily.MATTER:
            return Protocol.MATTER
        if fid == NodeFamily.ZIGBEE:
            return Protocol.ZIGBEE
        # NODESERVER family, or any id we don't recognise — PG3
        # plugin nodes report their slot id in this field.
        if fid and fid not in _CORE_FAMILY_IDS:
            return Protocol.NODE_SERVER
        return Protocol.UNKNOWN

    @property
    def is_thermostat(self) -> bool:
        """True if the node accepts climate-mode or setpoint commands."""
        return self._has_command(CMD_CLIMATE_MODE) or self._has_command(PROP_SETPOINT_HEAT)

    @property
    def is_lock(self) -> bool:
        """True for door/deadbolt locks.

        Two tells: nodedef accepts ``SECMD`` (Z-Wave / Insteon I2CS), or
        nodedef id contains ``"Lock"`` (IoX 6+ ``DoorLock`` variants that
        drive via ``DON``/``DOF``).
        """
        return self._has_command(CMD_SECURE) or "Lock" in self.nodedef_id

    @property
    def is_fan(self) -> bool:
        """True for multi-speed fan controllers (nodedef id contains ``"Fan"``).

        Fan nodes are a subset of dimmable (``FanLincMotor`` accepts
        ``DON`` with a ``{0, 25, 75, 100}`` subset), so platform
        classification should check ``is_fan`` **before** ``is_dimmable``.
        """
        return "Fan" in self.nodedef_id

    @property
    def is_dimmable(self) -> bool:
        """True if the node reports a multilevel ``ST`` state.

        Derived from the ``ST`` property editor: a binary subset
        (``{0, 100}``) → not dimmable; a multilevel range → dimmable.
        Relay nodedefs accept ``DON`` with an ignored level param, so
        ``DON``'s editor is unreliable — ``ST`` is the source of truth.
        """
        if self._nodedef is None:
            return False
        st_prop = self._nodedef.properties.get(PROP_STATUS)
        if st_prop is None or st_prop.editor_id is None:
            return False
        editor = self._profile.find_editor(st_prop.editor_id, self.family_id, self.instance_id)
        if editor is None or not editor.ranges:
            return False
        rng = editor.ranges[0]
        if rng.subset and len(rng.subset) <= 2:
            return False  # binary state — definitely not dimmable
        return not (rng.max is None or rng.max <= 1)

    @property
    def is_battery_node(self) -> bool:
        """True if the node reports ``BATLVL`` but no ``ST``.

        Battery-powered Insteon sensors (motion, leak, open/close) match
        this — they have no on/off primary state.
        """
        props = self._record.properties
        return PROP_BATTERY_LEVEL in props and PROP_STATUS not in props

    def _has_command(self, cmd_id: str) -> bool:
        """True if ``cmd_id`` appears in this node's ``cmds.accepts``."""
        if self._nodedef is None:
            return False
        return any(c.id == cmd_id for c in self._nodedef.cmds.accepts)

    # --- commanding ---------------------------------------------------

    async def send_command(self, command_id: str, *params: float | str) -> None:
        """Send a command, with editor-codec parameter validation.

        Each parameter is sent as ``/{value}/{uom}`` using the UOM its
        editor declares (the eisy web-UI convention — ``/cmd/DON/75/51``).
        Parameters whose editor carries no real unit (UOM ``"0"`` or
        unset) are sent bare.

        When the node has no resolved nodedef (dynamically provisioned
        Z-Wave/Z-Matter nodes whose ``UZW*`` defs aren't in
        ``/rest/profiles``), params pass through verbatim (numeric →
        int, no UOM) so the node stays controllable without validation.
        """
        if self._nodedef is None:
            passthrough: list[int | str] = [int(p) if isinstance(p, (int, float)) else p for p in params]
            await self._client.send_node_command(self.address, command_id, *passthrough)
            return
        encoded = encode_command_params(
            nodedef=self._nodedef,
            profile=self._profile,
            family_id=self.family_id,
            instance_id=self.instance_id,
            command_id=command_id,
            params=params,
            target_label=f"node {self.address!r}",
        )
        wire_args: list[int | str] = []
        for raw_value, uom in encoded:
            wire_args.append(raw_value)
            if uom and uom != "0":
                wire_args.append(uom)
        await self._client.send_node_command(self.address, command_id, *wire_args)

    # --- ergonomic wire-convention wrappers ---------------------------
    #
    # Each method below is a one-liner over :meth:`send_command` with the
    # IoX wire-convention command id baked in. Validation happens in the
    # editor codec; consumers never need to know the wire-level command
    # ids. Helpers stay deliberately thin — composite / policy logic
    # (e.g. setpoint min-gap) belongs in the consumer.

    async def set_climate_mode(self, mode: str | int) -> None:
        """Set HVAC mode. Accepts enum names (``"Heat"``, ``"Cool"``,
        ``"Auto"``, ``"Program Auto"``, ...) or raw ints. The editor
        for ``CLIMD`` enforces subset membership (e.g. excludes
        ``"Fan Only"`` on devices that don't support it)."""
        await self.send_command(CMD_CLIMATE_MODE, mode)

    async def set_climate_setpoint_heat(self, val: float) -> None:
        """Set the heat setpoint. The codec scales by ``prec`` (or
        doubles for legacy UOM-101 half-degree editors)."""
        await self.send_command(PROP_SETPOINT_HEAT, val)

    async def set_climate_setpoint_cool(self, val: float) -> None:
        """Set the cool setpoint."""
        await self.send_command(PROP_SETPOINT_COOL, val)

    async def set_fan_mode(self, mode: str | int) -> None:
        """Set fan mode. Accepts enum names (``"Auto"``, ``"On"``,
        ``"Auto High"``, ...) or raw ints."""
        await self.send_command(CMD_CLIMATE_FAN_SETTING, mode)

    async def secure_lock(self) -> None:
        """Issue a secure-lock command (Z-Wave / Insteon I2CS)."""
        await self.send_command(CMD_SECURE, 1)

    async def secure_unlock(self) -> None:
        """Issue a secure-unlock command."""
        await self.send_command(CMD_SECURE, 0)

    async def set_on_level(self, val: int) -> None:
        """Set the remembered on-level via ``OL`` (0-100 percent)."""
        await self.send_command(PROP_ON_LEVEL, val)

    async def set_ramp_rate(self, val: int) -> None:
        """Set the device's ramp rate.

        Insteon: 0-31 index into the IoX ramp-rate table. Z-Wave:
        seconds. The editor enforces the per-device range.
        """
        await self.send_command(PROP_RAMP_RATE, val)

    async def set_backlight(self, val: int | str) -> None:
        """Set keypad/switch backlight intensity.

        Two encodings driven by the BL editor's UOM: UOM 100 → 0-100%,
        UOM 25 → integer index (or enum-name string the codec resolves).
        """
        await self.send_command(CMD_BACKLIGHT, val)

    async def start_manual_dimming(self) -> None:
        """Begin manual dimming (legacy Insteon ``BMAN``).

        The IoX docs prefer the ``FADE_*`` family for new code.
        """
        await self.send_command(CMD_MANUAL_DIM_BEGIN)

    async def stop_manual_dimming(self) -> None:
        """End manual dimming (legacy Insteon ``SMAN``)."""
        await self.send_command(CMD_MANUAL_DIM_STOP)

    async def rename(self, name: str) -> None:
        """Rename this node. The controller emits a ``_3`` lifecycle frame
        with ``action="NN"`` on success."""
        await self._client.post_node_update(self.address, {"name": name, "nodeType": NodeType.NODE})

    def to_dict(self) -> dict[str, Any]:
        """Flatten this node to a JSON-compatible dict (record + protocol)."""
        payload = asdict(self._record)
        payload["protocol"] = self.protocol
        return payload

    # --- Z-Wave parameter surface ------------------------------------
    #
    # Z-Wave configuration parameters live on a dedicated wire path
    # (``/rest/(zmatter/)?zwave/node/{addr}/parameters/...``). The legacy
    # ``CONFIG`` accept command models only (NUM, VAL) — no slot for byte
    # size — so these helpers are the supported read/write surface.

    async def get_zwave_parameter(self, number: int) -> dict[str, int]:
        """Request parameter ``number``; return ``{parameter, size, value}``.

        Family id picks the wire prefix (``"4"`` → ``/rest/zwave/...``,
        ``"12"`` → ``/rest/zmatter/zwave/...``). Raises
        :class:`NodeCommandError` on non-Z-Wave nodes or controller
        failure; ``ISYResponseParseError`` on malformed bodies.
        """
        if self.family_id not in _ZWAVE_FAMILY_IDS:
            raise NodeCommandError(
                f"node {self.address!r} is not a Z-Wave node "
                f"(family={self.family_id!r}); parameters surface is "
                "Z-Wave-only"
            )
        zmatter = self.family_id == NodeFamily.ZMATTER_ZWAVE
        body = await self._client.get_zwave_parameter(self.address, number, zmatter=zmatter)
        parsed = _parse_zwave_parameter_response(self.address, number, body)
        _LOGGER.debug("Z-Wave get parameter on %s succeeded: %s", self.address, parsed)
        return parsed

    async def set_zwave_parameter(self, number: int, value: int, size: int) -> None:
        """Write parameter ``number`` (size 1/2/4 bytes) on this Z-Wave node.

        The post-write report arrives asynchronously on the WS stream.
        Raises :class:`NodeCommandError` on rejection so failures aren't
        silent.
        """
        if self.family_id not in _ZWAVE_FAMILY_IDS:
            raise NodeCommandError(
                f"node {self.address!r} is not a Z-Wave node "
                f"(family={self.family_id!r}); parameters surface is "
                "Z-Wave-only"
            )
        if size not in (1, 2, 4):
            raise NodeCommandError(f"Z-Wave parameter size must be 1, 2, or 4 bytes; got {size!r}")
        zmatter = self.family_id == NodeFamily.ZMATTER_ZWAVE
        body = await self._client.set_zwave_parameter(self.address, number, value, size, zmatter=zmatter)
        _check_rest_response_succeeded(
            self.address,
            body,
            context=(f"Z-Wave set parameter {number}={value} (size={size}) on {self.address!r}"),
        )
        _LOGGER.debug(
            "Z-Wave set parameter on %s succeeded: parameter=%d value=%d size=%d",
            self.address,
            number,
            value,
            size,
        )

    # --- Z-Wave lock-code surface ------------------------------------
    #
    # Wire paths come from PyISY 3.x — assumed valid on IoX 6+ without
    # captured proof; needs a tester with an enrolled Z-Wave lock for
    # confirmation. Lock codes are write-only (no "get code" surface).

    async def set_zwave_lock_code(self, user_num: int, code: int) -> None:
        """Program a Z-Wave lock's user-code slot. Raises
        :class:`NodeCommandError` on a failed envelope."""
        if self.family_id not in _ZWAVE_FAMILY_IDS:
            raise NodeCommandError(
                f"node {self.address!r} is not a Z-Wave node "
                f"(family={self.family_id!r}); lock-code surface is "
                "Z-Wave-only"
            )
        zmatter = self.family_id == NodeFamily.ZMATTER_ZWAVE
        body = await self._client.set_zwave_lock_code(self.address, user_num, code, zmatter=zmatter)
        _check_rest_response_succeeded(
            self.address,
            body,
            context=(f"Z-Wave set lock code user_num={user_num} on {self.address!r}"),
        )
        _LOGGER.debug(
            "Z-Wave set lock code on %s succeeded: user_num=%d",
            self.address,
            user_num,
        )

    async def delete_zwave_lock_code(self, user_num: int) -> None:
        """Clear a Z-Wave lock's user-code slot."""
        if self.family_id not in _ZWAVE_FAMILY_IDS:
            raise NodeCommandError(
                f"node {self.address!r} is not a Z-Wave node "
                f"(family={self.family_id!r}); lock-code surface is "
                "Z-Wave-only"
            )
        zmatter = self.family_id == NodeFamily.ZMATTER_ZWAVE
        body = await self._client.delete_zwave_lock_code(self.address, user_num, zmatter=zmatter)
        _check_rest_response_succeeded(
            self.address,
            body,
            context=(f"Z-Wave delete lock code user_num={user_num} on {self.address!r}"),
        )
        _LOGGER.debug(
            "Z-Wave delete lock code on %s succeeded: user_num=%d",
            self.address,
            user_num,
        )

    async def set_enabled(self, enabled: bool) -> None:
        """Enable or disable this node on the controller.

        On success the local ``enabled`` flag is updated optimistically;
        the controller also emits a ``_3`` ``action="EN"`` lifecycle.
        """
        await self._client.set_node_enabled(self.address, enabled)
        self._record.enabled = enabled


# --- Z-Wave parameter response helpers -----------------------------------
#
# Module-level so they can be exercised by parser-shape tests without
# constructing a Node + client. Both shapes were verified against PyISY
# 3.x's :class:`pyisy.nodes.Node.get_zwave_parameter` /
# :meth:`set_zwave_parameter` — the eisy/IoX side hasn't changed.


def _parse_zwave_parameter_response(address: str, number: int, body: str) -> dict[str, int]:
    """Decode the controller's ``<config>``/``<RestResponse>`` body.

    Success shape: ``<config paramNum="N" size="SZ" value="V"/>`` →
    ``{"parameter": N, "size": SZ, "value": V}`` (ints).
    Failure shape: ``<RestResponse succeeded="false">…`` → raises
    :class:`NodeCommandError` quoting the controller's status code.
    Anything else (truncated frame, unexpected root) → raises
    :class:`ISYResponseParseError`.
    """
    try:
        root = ET.fromstring(body)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError as exc:
        raise ISYResponseParseError(
            f"Z-Wave parameter response for {address!r} param {number} is not well-formed XML: {exc}"
        ) from exc
    if root.tag == "config":
        try:
            return {
                "parameter": int(root.attrib.get("paramNum", number)),
                "size": int(root.attrib["size"]),
                "value": int(root.attrib["value"]),
            }
        except (KeyError, ValueError) as exc:
            raise ISYResponseParseError(
                f"Z-Wave <config> response for {address!r} param {number} "
                f"is missing size/value or has non-integer attrs: "
                f"{root.attrib!r}"
            ) from exc
    if root.tag == "RestResponse":
        status = root.findtext("status", default="").strip()
        succeeded = root.attrib.get("succeeded", "true").lower() == "true"
        if not succeeded:
            raise NodeCommandError(
                f"Z-Wave get parameter {number} on {address!r} rejected by "
                f"controller (status={status or 'unknown'})"
            )
        # A succeeded RestResponse without <config> is unexpected — parse
        # error so the body surfaces for triage.
        raise ISYResponseParseError(
            f"Z-Wave get parameter {number} on {address!r} returned a "
            f"RestResponse envelope without a <config> payload: {body!r}"
        )
    raise ISYResponseParseError(
        f"Z-Wave parameter response for {address!r} param {number} has "
        f"unexpected root element {root.tag!r}: {body!r}"
    )


def _check_rest_response_succeeded(address: str, body: str, *, context: str) -> None:
    """Raise :class:`NodeCommandError` when ``<RestResponse>`` says no.

    A success ``<RestResponse succeeded="true"/>`` (or any body without
    a recognised RestResponse envelope) passes silently — controllers
    occasionally elide the envelope on success and we don't want to
    second-guess that. The conservative read is: only treat
    ``succeeded="false"`` as a hard failure.
    """
    try:
        root = ET.fromstring(body)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError:
        return
    if root.tag != "RestResponse":
        return
    if root.attrib.get("succeeded", "true").lower() == "true":
        return
    status = root.findtext("status", default="").strip()
    raise NodeCommandError(f"{context} rejected by controller (status={status or 'unknown'})")
