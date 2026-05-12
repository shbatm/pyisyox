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
from pyisyox.runtime._commands import NodeCommandError, encode_command_params

if TYPE_CHECKING:
    from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
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
        """Address of the tree-hierarchy parent (folder containing this node).

        Sourced from the IoX ``<parent>`` element. ``None`` for nodes at the
        root of the node tree. Matches the ``parent_address`` semantic on
        :class:`pyisyox.Folder`, :class:`pyisyox.Group`, and
        :class:`pyisyox.Program` — i.e., "what folder is this in?"

        Note: this is NOT the device primary for multi-button physical
        devices (KeypadLinc, RemoteLinc, FanLinc). For that, use
        :attr:`primary_address` — which derives from the IoX ``<pnode>``
        element. The two concepts are independent: a sub-button can be
        under a folder while also being a sub-node of a device primary.
        """
        return self._record.parent_address

    @property
    def primary_address(self) -> str | None:
        """Address of the device primary for sub-button nodes.

        Sourced from the IoX ``<pnode>`` element. For multi-button physical
        devices (KeypadLinc, RemoteLinc, FanLinc) the sub-button nodes carry
        the primary load node's address in ``<pnode>``; primaries carry
        their own address. We return ``None`` for primaries so the consumer
        can treat ``primary_address is not None`` as a "this is a sub-node"
        indicator and ``primary_address or address`` as "the canonical
        device-grouping address."

        PyISY 3.x exposed the same concept as ``Node.primary_node`` (which
        returned a string). The ``_address`` suffix here matches the
        convention used elsewhere in the package (``parent_address``,
        ``member_addresses``, ``controller_addresses``).
        """
        pnode = self._record.pnode
        if pnode is None or pnode == self._record.address:
            return None
        return pnode

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
    def protocol(self) -> Protocol:
        """Best-effort device-protocol classification, derived from ``family_id``.

        Returns a :class:`pyisyox.constants.Protocol` member (a
        ``StrEnum``, so it compares equal to its string value):

        * :attr:`Protocol.INSTEON` — family ``"1"``.
        * :attr:`Protocol.UPB` — family ``"2"``.
        * :attr:`Protocol.ZWAVE` — family ``"4"`` (legacy attached
          Z-Wave radio) or ``"12"`` (Z-Matter radio as a Z-Wave
          controller).
        * :attr:`Protocol.MATTER` — family ``"15"`` (Z-Matter radio
          as a Matter/Thread controller).
        * :attr:`Protocol.NODE_SERVER` — family ``"10"`` or any id
          outside the IoX core family set; PG3 plugin nodes report a
          slot id here, so we treat the whole space as node-server.
        * :attr:`Protocol.UNKNOWN` — recognised core family with no
          dedicated protocol mapping (RCS, Brultech, NCD, UDI, the
          group/auto families, folders) or no family id at all.

        This classifies the *transport*, not the device class — a
        plugin thermostat reads as ``NODE_SERVER`` here; use
        :attr:`is_thermostat` etc. for capability.
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
        # NODESERVER family, or any id we don't recognise — PG3
        # plugin nodes report their slot id in this field.
        if fid and fid not in _CORE_FAMILY_IDS:
            return Protocol.NODE_SERVER
        return Protocol.UNKNOWN

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
    def is_fan(self) -> bool:
        """True if the node is a multi-speed fan controller.

        Derived from the nodedef id substring ``"Fan"`` — matches
        the Insteon ``FanLincMotor`` sub-node (the FanLinc light
        side reports as ``DimmerLampOnly``, so light/fan separate
        cleanly per sub-address) and PG3 plugin fan nodedefs that
        follow the same naming convention.

        Fan nodes are a subset of dimmable nodes (``FanLincMotor``
        accepts ``DON`` with a level parameter restricted to the
        ``{0, 25, 75, 100}`` Off/Low/Medium/High subset), so
        callers classifying onto a HA-style ``Platform.FAN`` should
        check ``is_fan`` **before** ``is_dimmable``.
        """
        return "Fan" in self.nodedef_id

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
        """Set the device's remembered on-level via the legacy ``OL`` command.

        ``val`` is the **device-native** encoding:

        * **Insteon dimmers** — a 0-255 raw byte (the controller also
          reports ``OL`` as a UOM-100 byte; ``255`` = 100%).
        * **Z-Wave / Zigbee dimmers, KeypadLinc** — 0-100.

        The profile's ``I_OL`` editor only describes the 0-100%
        *display* slider (``max=100``), so it would wrongly reject the
        0-255 byte the Insteon ``/cmd/OL`` endpoint expects — the editor
        codec is therefore **not** applied here; the value is sent to
        ``/rest/nodes/{addr}/cmd/OL/{val}`` verbatim. Callers working in
        percentages scale to the device's encoding first (the consumer
        knows whether the ``OL`` property reports as a byte).

        Raises:
            NodeCommandError: when ``val`` is outside ``0-255`` (a
                coarse sanity bound; the controller enforces the real
                per-device range).
        """
        raw = int(val)
        if not 0 <= raw <= 255:
            raise NodeCommandError(f"node {self.address!r}: on-level {val!r} is out of range 0-255")
        await self._client.send_node_command(self.address, PROP_ON_LEVEL, raw)

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

    async def rename(self, name: str) -> None:
        """Rename this node.

        Wire shape: ``POST /api/nodes/{address}`` with
        ``{"name": "<str>", "nodeType": "node"}``. The IoX server
        emits a ``<control>_3</control>`` lifecycle event with
        ``action="NN"`` after a successful rename, so consumers
        listening through
        :meth:`Controller.add_node_lifecycle_listener` will see the
        change without polling.
        """
        await self._client.post_node_update(self.address, {"name": name, "nodeType": NodeType.NODE})

    async def set_enabled(self, enabled: bool) -> None:
        """Enable or disable this node on the controller.

        Wire shape: ``GET /rest/nodes/{address}/{enable|disable}``. A
        disabled node stays in the table but the controller stops
        polling / commanding it. On success the local record's
        :attr:`enabled` flag is updated optimistically (the controller
        also emits a ``<control>_3</control>`` ``action="EN"`` lifecycle
        event); on failure the underlying ``HTTPError`` propagates and
        the flag is left untouched.
        """
        await self._client.set_node_enabled(self.address, enabled)
        self._record.enabled = enabled
