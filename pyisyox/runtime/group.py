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

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from pyisyox.client import NodeType
from pyisyox.constants import INSTEON_STATELESS_NODEDEFID, PROP_STATUS

if TYPE_CHECKING:
    from pyisyox.client import GroupRecord, IoXClient, NodeRecord
    from pyisyox.schema.profile import Profile

#: Member nodedefs whose ``ST`` isn't a persistent state — motion
#: sensors, RemoteLincs, binary-alarm devices, etc. Skipped when
#: aggregating a scene's on/off state so a momentary or absent reading
#: from one of these doesn't drag :attr:`Group.group_all_on` to False.
_STATELESS_NODEDEF_IDS = frozenset(INSTEON_STATELESS_NODEDEFID)


def _is_on(record: NodeRecord) -> bool:
    """True iff this node currently reports a non-zero ``ST``."""
    st = record.properties.get(PROP_STATUS)
    return st is not None and str(st.value) not in ("", "0")


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

    def _is_stateless(self, record: NodeRecord) -> bool:
        return record.nodedef_id in _STATELESS_NODEDEF_IDS

    def _on_set(self) -> tuple[str, ...] | None:
        """Member addresses the scene drives *on*, or ``None`` for legacy.

        When ``/api/groups`` link targets resolved (see
        :attr:`pyisyox.client.GroupRecord.targets_resolved`) this is the
        subset of members whose scene intent is ``"on"`` — the only
        members whose ``ST`` defines whether the scene is active.
        ``off`` / ``discard`` members and members the scene merely
        fires a one-shot command at are excluded. A *resolved* scene
        with no ``"on"`` members (fire-only / config-only / auto-DR)
        yields an empty tuple → reads OFF, matching the admin console.

        ``None`` means targets are unresolved (``/api/groups`` absent,
        or an ambiguous link) → callers fall back to the legacy
        all-member aggregate so behaviour never regresses.
        """
        if not self._record.targets_resolved:
            return None
        return tuple(addr for addr, intent in self._record.member_intents.items() if intent == "on")

    @property
    def has_state_target(self) -> bool:
        """Whether the scene maintains any member on/off state.

        ``True`` when link targets resolved and at least one member has
        an ``on``/``off`` intent. A *resolved* scene with only fire-only
        / config links (``cmd`` ``BL``/``BEEP``/…, or empty) → ``False``:
        it has no steady state, so a consumer should model it as a
        momentary **button**, not a switch. When targets are unresolved
        we can't tell, so assume ``True`` (the safe default — keep it a
        stateful scene).
        """
        if not self._record.targets_resolved:
            return True
        return any(intent in ("on", "off") for intent in self._record.member_intents.values())

    @property
    def group_all_on(self) -> bool:
        """True iff every on-target member currently reports an "on" state.

        Computed on access from the controller's node registry.
        Stateless members — motion sensors, RemoteLincs, binary-alarm
        devices, see :data:`_STATELESS_NODEDEF_IDS` — are excluded;
        their ``ST`` isn't a persistent state.

        When ``/api/groups`` link targets resolved, the aggregate is
        over the scene's **on-target** members only (see
        :meth:`_on_set`) — so a radio-style keypad scene (one button
        on-target, the rest driven off) tracks correctly instead of
        being structurally never-all-on. Otherwise it falls back to the
        legacy all-member behaviour. Returns ``False`` when the group
        has no node-registry reference, the (on-target / member) set is
        empty, a member is missing from the registry, or any counted
        member's ``ST`` is missing or zero.

        Cheap: ``O(N)``, computed on read — the underlying ``ST`` values
        mutate in place via the WS dispatcher, so each access reflects
        the latest state.
        """
        if self._nodes is None:
            return False
        on_set = self._on_set()
        addresses = on_set if on_set is not None else self._record.member_addresses
        if not addresses:
            return False
        saw_stateful = False
        for addr in addresses:
            record = self._nodes.get(addr)
            if record is None:
                return False
            if self._is_stateless(record):
                continue
            saw_stateful = True
            if not _is_on(record):
                return False
        return saw_stateful

    @property
    def group_any_on(self) -> bool:
        """True iff at least one on-target member currently reports "on".

        Companion to :attr:`group_all_on`; this is the aggregation HA
        scene-switch consumers want for their ``is_on``. When
        ``/api/groups`` link targets resolved it considers only the
        scene's **on-target** members (see :meth:`_on_set`), so a scene
        reads on iff a member it actually drives on is on — not merely
        because some always-lit keypad button is non-zero. Otherwise it
        falls back to the legacy "any stateful member non-zero"
        behaviour (what ``pyisy.Group.status`` did). Stateless members
        and members not in the registry are skipped.

        Returns ``False`` with no node-registry reference, an empty
        (on-target / member) set, or when every counted member's ``ST``
        is missing or zero. Cheap: ``O(N)``, computed on read.
        """
        if self._nodes is None:
            return False
        on_set = self._on_set()
        addresses = on_set if on_set is not None else self._record.member_addresses
        for addr in addresses:
            record = self._nodes.get(addr)
            if record is None or self._is_stateless(record):
                continue
            if _is_on(record):
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
        await self._client.post_node_update(self.address, {"name": name, "nodeType": NodeType.GROUP})

    def to_dict(self) -> dict[str, Any]:
        """Flatten this scene to a JSON-compatible dict.

        Adds the live aggregate flags (``group_all_on`` / ``group_any_on``)
        on top of the structural record so a snapshot reflects whether
        the scene is currently active.
        """
        payload = asdict(self._record)
        payload["group_all_on"] = self.group_all_on
        payload["group_any_on"] = self.group_any_on
        payload["has_state_target"] = self.has_state_target
        return payload

    def __repr__(self) -> str:
        return f"Group(address={self.address!r}, name={self.name!r}, members={len(self.member_addresses)})"
