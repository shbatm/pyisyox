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

from pyisyox.constants import PROP_STATUS

if TYPE_CHECKING:
    from pyisyox.client import GroupRecord, IoXClient, NodeRecord
    from pyisyox.schema.profile import Profile


class Group:
    """User-facing handle for one group / scene in the controller."""

    __slots__ = ("_client", "_nodes", "_profile", "_record")

    def __init__(
        self,
        record: GroupRecord,
        profile: Profile,
        client: IoXClient,
        nodes: dict[str, NodeRecord] | None = None,
    ) -> None:
        """Bind the parsed :class:`GroupRecord` to a profile + client.

        Args:
            record: The parsed group record from ``/rest/nodes`` XML.
            profile: The :class:`Profile` (held for symmetry with
                :class:`Node` and future scene-command validation).
            client: The HTTP client used to send group commands.
            nodes: Optional reference to the controller's
                ``loaded.nodes`` registry. When supplied, enables
                :attr:`group_all_on` to compute on access. Without it
                that property always returns ``False``.
        """
        self._record = record
        self._profile = profile
        self._client = client
        self._nodes = nodes

    @classmethod
    def from_record(
        cls,
        record: GroupRecord,
        profile: Profile,
        client: IoXClient,
        nodes: dict[str, NodeRecord] | None = None,
    ) -> Group:
        """Construct a Group from a parsed record.

        Pass ``nodes`` (the controller's ``loaded.nodes`` dict) to enable
        the :attr:`group_all_on` derived property. Without it the group
        is purely command-issuing.
        """
        return cls(record=record, profile=profile, client=client, nodes=nodes)

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
        Order matches the controller's declaration order. Includes
        both controllers and responders; use
        :attr:`controller_addresses` for the controller subset.
        """
        return self._record.member_addresses

    @property
    def controller_addresses(self) -> tuple[str, ...]:
        """Subset of :attr:`member_addresses` that the controller flags
        as scene controllers (``<link type="16">``).

        Empty when the group has no explicit controller (e.g. virtual
        scenes / SmartLinc-style automation groups).
        """
        return self._record.controller_addresses

    @property
    def group_all_on(self) -> bool:
        """True iff every member node currently reports an "on" state.

        Computed on access from the controller's node registry. Returns
        ``False`` when:

        * the group was constructed without a node-registry reference
          (the optional ``nodes`` arg to :meth:`from_record`);
        * any member is not currently in the registry (defensive — the
          member may have been removed since load and the controller
          hasn't surfaced the lifecycle event yet);
        * any member's ``ST`` property is missing or zero.

        Cheap: ``O(N)`` where N is the member count, only computed
        when read. No event-subscription plumbing — the underlying
        ``ST`` values mutate in place via the WS dispatcher, so each
        access reflects the latest state.
        """
        if self._nodes is None or not self._record.member_addresses:
            return False
        for addr in self._record.member_addresses:
            record = self._nodes.get(addr)
            if record is None:
                return False
            st = record.properties.get(PROP_STATUS)
            if st is None or str(st.value) in ("", "0"):
                return False
        return True

    @property
    def group_any_on(self) -> bool:
        """True iff at least one member node currently reports an "on" state.

        Companion to :attr:`group_all_on`; this is the aggregation HA
        scene-switch consumers want for their ``is_on`` — the legacy
        ``pyisy.Group.status`` did the same thing (non-zero on any
        responder).

        Returns ``False`` when:

        * the group was constructed without a node-registry reference;
        * the group has no members;
        * every present member's ``ST`` property is missing or zero
          (missing members are skipped, not treated as 'off' — see
          :attr:`group_all_on` for the strict variant).

        Cheap: ``O(N)`` where N is the member count, only computed
        when read.
        """
        if self._nodes is None or not self._record.member_addresses:
            return False
        for addr in self._record.member_addresses:
            record = self._nodes.get(addr)
            if record is None:
                continue
            st = record.properties.get(PROP_STATUS)
            if st is not None and str(st.value) not in ("", "0"):
                return True
        return False

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

    async def rename(self, name: str) -> None:
        """Rename this group / scene.

        Wire shape: ``POST /api/nodes/{address}`` with
        ``{"name": "<str>", "nodeType": "group"}``. The ``nodeType``
        field is required by the server even though the address
        already disambiguates — without it the call is rejected.
        """
        await self._client.post_node_update(self.address, {"name": name, "nodeType": "group"})

    def __repr__(self) -> str:
        return f"Group(address={self.address!r}, name={self.name!r}, members={len(self.member_addresses)})"
