"""Runtime ``Node`` — wraps a :class:`NodeRecord` + :class:`NodeDef` + client.

The :class:`Node` is the primary user-facing handle for a single device.
It exposes the structural fields and current property values
(populated by :class:`pyisyox.client.IoXClient`'s initial load and
later updated by the WebSocket dispatcher) plus a
:meth:`Node.send_command` that:

1. Looks the command up in the node's :class:`NodeDef` (under
   ``cmds.accepts``).
2. Validates each parameter against the editor that command parameter
   references — using the bidirectional codec from
   :mod:`pyisyox.schema.editor`. Enum names get translated to their
   raw integers; subset constraints are enforced; out-of-range numeric
   values raise before any HTTP traffic.
3. Issues the legacy XML command endpoint
   ``GET /rest/nodes/{addr}/cmd/{cmd_id}[/{p1}[/{p2}...]]`` via the
   client. The response is a small ``<RestResponse/>`` envelope which
   we don't need to decode beyond confirming the HTTP status.

Only the legacy ``/rest/nodes/{addr}/cmd/...`` surface exists for
sending node commands — there is no ``/api/*`` equivalent observed in
captures, so we go through the legacy XML path. Auth still works:
both :class:`PortalAuth` JWT and :class:`LocalAuth` HTTP basic accept
``/rest/*`` requests.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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
    NodeFlag,
)
from pyisyox.runtime._commands import NodeCommandError, encode_command_params

if TYPE_CHECKING:
    from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
    from pyisyox.schema.nodedef import NodeDef
    from pyisyox.schema.profile import Profile

__all__ = ["Node", "NodeCommandError"]


#: Family-id values that identify a Z-Wave / Z-Matter native node. The
#: legacy split is preserved on IoX 6 — `12` is the original Z-Wave radio,
#: `15` is the Matter / Z-Wave 800-series Z-Matter radio.
_FAMILY_ZWAVE = "12"
_FAMILY_ZMATTER_ZWAVE = "15"
#: Family `1` is native Insteon (and a few legacy paths).
_FAMILY_INSTEON = "1"
#: Family `2` is X10.
_FAMILY_X10 = "2"
#: Family `4` is Zigbee.
_FAMILY_ZIGBEE = "4"

#: All known native (non-plugin) family ids, for protocol classification.
_NATIVE_FAMILY_IDS = frozenset(
    {_FAMILY_INSTEON, _FAMILY_ZWAVE, _FAMILY_ZMATTER_ZWAVE, _FAMILY_ZIGBEE, _FAMILY_X10}
)


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
        """Store the components needed for state reads and command sends.

        Args:
            record: The structural + merged-property data for this node.
            nodedef: The resolved nodedef, or ``None`` if the
                ``(nodedef_id, family, instance)`` triple didn't match
                anything in the profile (e.g. a brand-new plugin
                whose profile hasn't been loaded yet).
            profile: The parsed :class:`Profile`. Used to resolve
                editors scoped to this node's family/instance for
                command-parameter validation.
            client: The :class:`IoXClient` that owns the HTTP session.
                Calls go through ``client.send_node_command``.
        """
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
        """Address of the parent node (for KeypadLinc buttons, the primary
        load; for plugin sub-nodes, the controller). ``None`` for primaries."""
        return self._record.parent_address

    @property
    def primary_node(self) -> str | None:
        """Alias for :attr:`parent_address`.

        The IoX REST surface and the legacy PyISY 3.x consumer API both
        used the term "primary node" for the address of the device's
        root node. Kept here so consumers don't have to translate.
        """
        return self._record.parent_address

    @property
    def enabled(self) -> bool:
        """Whether the eisy considers this node active."""
        return self._record.enabled

    @property
    def properties(self) -> dict[str, NodePropertyValue]:
        """Live property values, keyed by property id (e.g. ``"ST"``).

        For native nodes these are merged from ``/api/nodes`` + the
        ``/rest/status`` overlay during initial load. Plugin nodes get
        all values from ``/rest/status``. WebSocket events update them
        in place at runtime via :class:`pyisyox.runtime.EventDispatcher`.
        """
        return self._record.properties

    @property
    def status(self) -> NodePropertyValue | None:
        """Shortcut for :attr:`properties`\\ ``[PROP_STATUS]`` — the
        node's primary status reading (``"ST"``).

        Returns ``None`` when the node hasn't reported ST yet (common
        for write-only Insteon controllers and plugin nodes that don't
        advertise ST). Consumers that want a scalar should read
        ``node.status.value`` (a string) and parse it themselves;
        the property keeps the structured shape so callers can also
        reach ``.uom``, ``.formatted``, etc.
        """
        return self._record.properties.get(PROP_STATUS)

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

        ``flag`` may be a single :class:`NodeFlag` member or an OR'd
        combination (e.g. ``NodeFlag.NEW | NodeFlag.IN_ERR``); the check
        is bitwise-AND against all requested bits, so a combined value
        only matches when every bit is present.

        Example::

            from pyisyox.constants import NodeFlag

            if node.has_flag(NodeFlag.DEVICE_ROOT):
                ...
        """
        return (self._record.flag & int(flag)) == int(flag)

    # --- introspection (derived) --------------------------------------

    @property
    def protocol(self) -> str:
        """Best-effort protocol classification.

        Returned strings: ``"insteon"``, ``"zwave"``, ``"zigbee"``,
        ``"x10"``, ``"node_server"``, ``"unknown"``. Derived from
        ``family_id`` (a numeric IoX family id for native devices,
        slot id for plugins).

        For PG3 plugin nodes the family id is the plugin's slot
        number (e.g. ``"10"`` for Flume) — those return
        ``"node_server"`` regardless of which physical protocol the
        plugin abstracts over.
        """
        fid = self.family_id
        if fid == _FAMILY_INSTEON:
            return "insteon"
        if fid in (_FAMILY_ZWAVE, _FAMILY_ZMATTER_ZWAVE):
            return "zwave"
        if fid == _FAMILY_ZIGBEE:
            return "zigbee"
        if fid == _FAMILY_X10:
            return "x10"
        # Plugin slots are anything that isn't a known native family id.
        if fid and fid not in _NATIVE_FAMILY_IDS:
            return "node_server"
        return "unknown"

    @property
    def is_thermostat(self) -> bool:
        """True if the node accepts climate-mode or setpoint commands.

        Derived from ``nodedef.cmds.accepts`` (protocol-agnostic), so a
        PG3 plugin thermostat reads as a thermostat the same way a
        native Insteon 2441ZTH does.
        """
        return self._has_command(CMD_CLIMATE_MODE) or self._has_command(PROP_SETPOINT_HEAT)

    @property
    def is_lock(self) -> bool:
        """True if the node is a door / deadbolt lock.

        Two tells, either is sufficient:

        * The nodedef accepts the secure-lock (``SECMD``) verb — Z-Wave
          and Insteon I2CS locks both expose this.
        * The nodedef id contains the substring ``"Lock"`` — covers
          IoX 6+ ``DoorLock`` / ``DoorLock_ADV`` / equivalents that
          drive the lock via ``DON`` / ``DOF`` rather than ``SECMD``.
        """
        return self._has_command(CMD_SECURE) or "Lock" in self.nodedef_id

    @property
    def is_dimmable(self) -> bool:
        """True if the node reports a multilevel ``ST`` (status) state.

        Derived from the ``ST`` property's editor on the resolved
        nodedef:

        * ``I_OL_RELAY`` / similar editors with a binary subset
          (``{0, 100}``) → on/off only → **not** dimmable.
        * ``I_OL`` / similar editors with a multilevel range (e.g.
          ``[0, 100]`` or ``[0, 255]``) and no binary subset →
          dimmable.

        Relay nodedefs accept ``DON`` with an optional level parameter
        too (the controller ignores the level), so checking ``DON``'s
        parameters isn't reliable. The ST editor is the source of
        truth — it's what the device reports its own state through.
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
        """True for nodes that report battery level but no on/off status.

        Battery-powered Insteon sensors (motion, leak, open/close) follow
        this pattern — they advertise ``BATLVL`` and protocol-specific
        sub-properties but no ``ST`` because they don't have an on/off
        primary state.
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
        """Send a command to this node, with editor-codec parameter validation.

        Each positional arg is matched against the corresponding
        parameter slot on the command's nodedef definition. Enum names
        ("Heat", "Authorized") are accepted in addition to integers and
        translated through the editor's ``names`` map. Out-of-subset or
        out-of-range values raise :class:`NodeCommandError` before any
        HTTP traffic.

        Args:
            command_id: The IoX command id (e.g. ``"DON"``, ``"DOF"``,
                ``"CLISPC"``, ``"DISCOVER"``).
            *params: Positional command arguments. Number must match
                the command's parameter count (or be zero for
                parameterless commands like ``DOF``).

        Raises:
            NodeCommandError: When the nodedef is unresolved, the
                command id isn't on this node's accept list, the
                parameter count is wrong, or any parameter fails
                editor validation.
        """
        encoded = encode_command_params(
            nodedef=self._nodedef,
            profile=self._profile,
            family_id=self.family_id,
            instance_id=self.instance_id,
            command_id=command_id,
            params=params,
            target_label=f"node {self.address!r}",
        )
        await self._client.send_node_command(self.address, command_id, *encoded)

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
        """Set the device's remembered on-level.

        Valid range is **per device** — the editor codec enforces the
        actual bounds via the ``OL`` property's editor on this node's
        nodedef:

        * Insteon dimmers: raw 0-255 (UOM 100, raw-byte encoded)
        * Z-Wave dimmers: 0-100 (UOM 100, percentage)
        * KeypadLinc relay/keypad-button: 0-100 (different editor)

        Pass the integer the device's editor expects. The codec raises
        :class:`pyisyox.runtime.NodeCommandError` on out-of-range; no
        pre-validation here, the codec is the source of truth.
        """
        await self.send_command(PROP_ON_LEVEL, val)

    async def set_ramp_rate(self, val: int) -> None:
        """Set the device's ramp rate.

        Insteon: 0-31 index into the IoX ramp-rate table. Z-Wave:
        seconds. The editor enforces the per-device range.
        """
        await self.send_command(PROP_RAMP_RATE, val)

    async def set_backlight(self, val: int | str) -> None:
        """Set keypad / switch backlight intensity.

        Two encoding modes selected by the BL editor's UOM:

        * **UOM 100 (percentage)** — DimmerSwitch / RelaySwitch / etc.
          Pass 0-100.
        * **UOM 25 (index)** — KeypadDimmer / KeypadRelay / KeypadButton.
          Pass an integer index into the IoX backlight table, or — if
          the profile's BL editor carries the enum names — the
          human-readable label string (the codec resolves it).

        The display-label list is *not* mirrored in pyisyox; consumers
        wanting the labels can iterate
        ``editor.range_for("25").names`` against the node's BL editor.
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
