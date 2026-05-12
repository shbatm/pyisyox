"""Node definition dataclasses for IoX devices.

A :class:`NodeDef` describes the static behaviour of a class of nodes: which
properties exist, which commands are accepted/sent, and which links the node
supports. The same shape applies to native Insteon/Z-Wave nodedefs and PG3
plugin nodedefs — there is no plugin-only field. The structural
key into the lookup table is ``(nodedef_id, family_id, instance_id)``.

A live :class:`Property` value (raw + formatted + uom) is reported by the
controller via ``/api/nodes`` (for native nodes), ``/rest/status`` (the
canonical full table), or WebSocket event frames; it is *not* part of the
nodedef and is kept here only as a separate dataclass.

Source schema: ``/rest/profiles`` instance ``nodedefs[]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyisyox.schema.cmd import Command


@dataclass(slots=True)
class Property:
    """A live property value reported by the controller for a node.

    Attributes:
        id: Property id (e.g. ``"ST"``, ``"GV1"``).
        value: Raw value as reported by the controller (string form keeps
            controller-emitted precision).
        formatted: Human-readable value (e.g. ``"0.6839 US gallons"``).
        uom: Unit-of-measure id reported alongside the value.
        prec: Decimal precision applied to ``value`` (None when not provided).
        name: Optional display name override (often empty — the nodedef-level
            ``NodeProperty.name`` is the authoritative label).
    """

    id: str
    value: str
    formatted: str = ""
    uom: str = ""
    prec: int | None = None
    name: str = ""


@dataclass(slots=True)
class NodeProperty:
    """A property slot defined on a nodedef.

    Attributes:
        id: Property id (e.g. ``"ST"``, ``"OL"``, ``"CLISPC"``, ``"GV1"``).
        editor_id: Reference to the editor governing this property's
            display and (where applicable) write-side validation.
        name: Human-readable label, inline-resolved by the controller
            (e.g. ``"Current"`` for Flume's ``GV1``, ``"On Level"`` for
            Insteon's ``OL``). Authoritative source.
        hide: Hint that the property should not be surfaced in default UIs.
    """

    id: str
    editor_id: str
    name: str = ""
    hide: bool = False


@dataclass(slots=True)
class NodeCommands:
    """Commands a nodedef sends and accepts.

    Attributes:
        sends: Commands the node *emits* — useful as trigger sources
            (e.g. ``OnOffControl`` sends ``DON``/``DOF`` on physical press).
        accepts: Commands the node *receives* — drive the node's controllable
            HA platform (light/switch/climate/lock/cover/button).
    """

    sends: list[Command] = field(default_factory=list)
    accepts: list[Command] = field(default_factory=list)


@dataclass(slots=True)
class NodeLinks:
    """Control and response link references on a nodedef."""

    ctl: list[str] = field(default_factory=list)
    rsp: list[str] = field(default_factory=list)


@dataclass(slots=True)
class NodeDef:
    """The static definition of a node class.

    Attributes:
        id: Nodedef identifier (e.g. ``"KeypadDimmer_ADV"``, ``"Thermostat"``,
            ``"flume2"``, ``"controller"``).
        family_id: Family id this nodedef belongs to (``"1"`` for Insteon,
            ``"4"`` for Z-Wave, plugin slot id for PG3 nodedefs).
        instance_id: Instance id within the family (typically equal to
            ``family_id`` for built-in families and equal to the plugin slot
            for PG3 instances).
        properties: Property slots, keyed by property id.
        cmds: Sent and accepted commands.
        nls_key: Reference key into the NLS string table (e.g. ``"flume2"``);
            pyisyox does not need to resolve this — every visible string is
            already inline-resolved in property/command ``name`` fields and
            in WS event frames.
        links: Control and response link references.
    """

    id: str
    family_id: str
    instance_id: str
    properties: dict[str, NodeProperty] = field(default_factory=dict)
    cmds: NodeCommands = field(default_factory=NodeCommands)
    nls_key: str | None = None
    links: NodeLinks = field(default_factory=NodeLinks)

    @classmethod
    def from_json(cls, raw: dict, family_id: str, instance_id: str) -> NodeDef:
        """Build a :class:`NodeDef` from a JSON object scoped to its family/instance."""
        props_in = raw.get("properties", []) or []
        properties: dict[str, NodeProperty] = {}
        for p in props_in:
            pid = p.get("id")
            if not pid:
                continue
            properties[pid] = NodeProperty(
                id=pid,
                editor_id=p.get("editor", ""),
                name=p.get("name", ""),
                hide=bool(p.get("hide", False)),
            )

        cmds_in = raw.get("cmds", {}) or {}
        cmds = NodeCommands(
            sends=[Command.from_json(c) for c in cmds_in.get("sends", []) or []],
            accepts=[Command.from_json(c) for c in cmds_in.get("accepts", []) or []],
        )

        def _link_ids(items: list) -> list[str]:
            return [ln.get("id", "") if isinstance(ln, dict) else str(ln) for ln in items or []]

        links_in = raw.get("links", {}) or {}
        links = NodeLinks(
            ctl=_link_ids(links_in.get("ctl", [])),
            rsp=_link_ids(links_in.get("rsp", [])),
        )

        return cls(
            id=raw["id"],
            family_id=family_id,
            instance_id=instance_id,
            properties=properties,
            cmds=cmds,
            nls_key=raw.get("nls"),
            links=links,
        )

    @property
    def lookup_key(self) -> tuple[str, str, str]:
        """The ``(nodedef_id, family_id, instance_id)`` join key used to
        match a node from ``/api/nodes`` to its definition.
        """
        return (self.id, self.family_id, self.instance_id)
