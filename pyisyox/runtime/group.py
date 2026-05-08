"""Runtime ``Group`` — IoX scenes (a.k.a. controller-side groups).

A group represents a controller-managed collection of nodes — the IoX
"scene" concept. Sending a command to the group's address is the same
wire shape as sending to a node (``GET /rest/nodes/{addr}/cmd/{cmd}``);
the controller broadcasts to every member.

Group commands are **sent raw without editor-codec validation**: the
``nodeDefId`` attribute on a ``<group>`` element (typically
``"InsteonDimmer"``) is a scene-class label, not a profile-level
nodedef, so there's no editor table to validate parameters against.
The well-known group commands are documented in the IoX REST guide:
``DON`` (with optional level), ``DOF``, ``DFON`` / ``DFOF`` (fast on/
off), ``BRT`` / ``DIM`` (manual brighten/dim). Pre-encode any
parameters as integers — the same shape :class:`pyisyox.runtime.Node`
produces internally.

Groups don't carry live property state of their own — there's no
``properties`` dict — because the wire-level group has no observable
attribute beyond its membership. State events flow through individual
member nodes.

Sourced from ``<group flag="132">`` elements in the legacy
``/rest/nodes`` XML. ``flag="12"`` (the special "ISY" group
representing the controller itself) is filtered out at parse time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyisyox.client import GroupRecord, IoXClient
    from pyisyox.schema.profile import Profile


class Group:
    """User-facing handle for one group / scene in the controller."""

    __slots__ = ("_client", "_profile", "_record")

    def __init__(self, record: GroupRecord, profile: Profile, client: IoXClient) -> None:
        """Bind the parsed :class:`GroupRecord` to a profile + client.

        ``profile`` is held for symmetry with :class:`Node` — current
        send-path doesn't consult it, but future enhancements (e.g. a
        well-known scene-command table or stricter param validation)
        will.
        """
        self._record = record
        self._profile = profile
        self._client = client

    @classmethod
    def from_record(cls, record: GroupRecord, profile: Profile, client: IoXClient) -> Group:
        """Construct a Group from a parsed record."""
        return cls(record=record, profile=profile, client=client)

    # --- introspection ------------------------------------------------

    @property
    def address(self) -> str:
        """Group address — usually a 5-digit integer string or ``"ADR####"``
        for special groups like ``~zAuto DR``."""
        return self._record.address

    @property
    def name(self) -> str:
        """User-assigned scene name."""
        return self._record.name

    @property
    def nodedef_id(self) -> str:
        """Scene-class label (``"InsteonDimmer"`` etc.). Not a real
        profile nodedef — see module docstring."""
        return self._record.nodedef_id

    @property
    def family_id(self) -> str:
        """Family id — usually ``"6"`` (Insteon group family)."""
        return self._record.family_id

    @property
    def instance_id(self) -> str:
        """Instance id within the family."""
        return self._record.instance_id

    @property
    def parent_address(self) -> str | None:
        """Address of the parent folder, or ``None`` if at the top level."""
        return self._record.parent_address

    @property
    def member_addresses(self) -> tuple[str, ...]:
        """Addresses of the nodes that belong to this group.

        Sourced from the ``<members>`` element in ``/rest/nodes`` XML.
        Order matches the controller's declaration order.
        """
        return self._record.member_addresses

    # --- commanding ---------------------------------------------------

    async def send_command(self, command_id: str, *params: int) -> None:
        """Send a command to every member of this group.

        Wire shape: ``GET /rest/nodes/{group_addr}/cmd/{command_id}[/{p1}...]``.
        The controller broadcasts to each member; results aren't
        returned per-member.

        Unlike :meth:`Node.send_command`, parameters are **not**
        validated through the editor codec — group nodedefs aren't
        profile-resolvable. Pass already-encoded integers; consumers
        are responsible for sanity checks (e.g. clamp on-level to
        0-100). Common usage:

        * ``await group.send_command("DON")`` — turn the scene on
          to its programmed level
        * ``await group.send_command("DON", 75)`` — explicit on-level
        * ``await group.send_command("DOF")``
        * ``await group.send_command("DFON")`` / ``"DFOF"`` — fast
        * ``await group.send_command("BRT")`` / ``"DIM"`` — manual
          brighten/dim step
        """
        await self._client.send_node_command(self.address, command_id, *params)

    def __repr__(self) -> str:
        return f"Group(address={self.address!r}, name={self.name!r}, members={len(self.member_addresses)})"
