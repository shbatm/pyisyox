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

if TYPE_CHECKING:
    from pyisyox.client import IoXClient, NodePropertyValue, NodeRecord
    from pyisyox.schema.nodedef import NodeDef
    from pyisyox.schema.profile import Profile


class NodeCommandError(Exception):
    """Raised when a command can't be sent — unknown command id, missing
    parameter, validation failure, or no nodedef resolved for this node."""


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
    def nodedef(self) -> NodeDef | None:
        """The resolved :class:`NodeDef`, or ``None`` if unresolved."""
        return self._nodedef

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
        # Late import — _commands depends on NodeCommandError defined above.
        from pyisyox.runtime._commands import encode_command_params  # noqa: PLC0415

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
