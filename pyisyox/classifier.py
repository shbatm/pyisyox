"""HA platform classifier for IoX nodedefs.

This module produces a classification of an IoX :class:`~pyisyox.schema.nodedef.NodeDef`
into Home Assistant platform contributions. It is the **fallback tier** of
the two-tier strategy described in the modernization plan:

1. *Type-based classification* (consumer-side, e.g. hacs-isy994) — primary
   path. Native Insteon/Z-Wave nodes carry a real ``type`` string
   (``"1.65.69.0"`` for KeypadLinc dimmer, etc.) that hacs-isy994 already
   maps to platforms with hardware-aware nuance. Preserve that path.
2. *Nodedef-based classification* (this module) — fallback. Fires when the
   consumer's type-based lookup returns no match, which in practice means
   PG3 plugin nodes and any future device class without a hardcoded
   mapping. The current HA core integration dumps these all into
   ``sensor``; this classifier escapes that trap.

The classification is **three-axis** and produces a set of HA platform
contributions, not a single platform pick:

* ``controllable`` — at most one of light/switch/climate/lock/cover/
  alarm_control_panel, derived from ``cmds.accepts``.
* ``triggers`` — every command in ``cmds.sends`` (the node *emits* these
  events; e.g., an Insteon ``OnOffControl`` paddle that sends ``DON``/
  ``DOF`` on press becomes a HA ``device_trigger`` source).
* ``buttons`` — accept commands pressable with **no arguments**:
  parameterless, or every parameter ``optional`` (the controller fills in
  defaults — Insteon ``BEEP`` is the canonical case: one optional
  ``level`` param). Excludes ``QUERY`` (implicit on every node) and any
  command claimed by the controllable platform. Surface as HA ``button``
  entities — press = send the verb with zero args.
* ``parameterized_commands`` — accept commands with at least one
  *required* parameter (same QUERY / controllable exclusions). These
  can't be plain buttons: each parameter carries an ``editor`` ref whose
  shape drives what input entity it needs. Consumers that don't yet
  handle them can ignore this bucket.
* ``readings`` — one entity per property, after filtering out properties
  already represented by the controllable platform (e.g., ``ST``/``OL``/
  ``RR`` on a light are the light's state, not separate sensors).
* ``aux_controls`` — the unified successor to the
  ``readings`` / ``parameterized_commands`` / ``buttons`` split: one
  :class:`AuxControl` per logical control, with a status and its
  ``init``-linked write command folded together (read/write coalesced).
  New consumers should prefer this; the three legacy buckets stay
  populated unchanged for now.

One HA *device* per node aggregates entities from these buckets.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from pyisyox.constants import (
    CMD_ALARM_ARM,
    CMD_ALARM_DISARM,
    CMD_BRIGHTEN,
    CMD_CLIMATE_FAN_SETTING,
    CMD_CLIMATE_MODE,
    CMD_DIM,
    CMD_FADE_DOWN,
    CMD_FADE_STOP,
    CMD_FADE_UP,
    CMD_OFF,
    CMD_ON,
    CMD_QUERY,
    CMD_SECURE,
    PROP_HEAT_COOL_STATE,
    PROP_ON_LEVEL,
    PROP_RAMP_RATE,
    PROP_SCHEDULE_MODE,
    PROP_SETPOINT_COOL,
    PROP_SETPOINT_COOL_DELTA,
    PROP_SETPOINT_HEAT,
    PROP_SETPOINT_HEAT_DELTA,
    PROP_STATUS,
    PROP_TEMPERATURE,
    UOM_BOOLEAN,
    UOM_BYTE,
    UOM_INDEX,
    UOM_ON_OFF,
    UOM_OPEN_CLOSED,
    UOM_PERCENTAGE,
)
from pyisyox.schema.cmd import Command
from pyisyox.schema.editor import Editor
from pyisyox.schema.nodedef import NodeDef, NodeProperty


class ControllablePlatform(StrEnum):
    """The single controllable HA platform a nodedef may map to."""

    LIGHT = "light"
    SWITCH = "switch"
    CLIMATE = "climate"
    LOCK = "lock"
    COVER = "cover"
    ALARM_CONTROL_PANEL = "alarm_control_panel"


class ReadingPlatform(StrEnum):
    """HA platform for a property reading entity."""

    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"


@dataclass(slots=True)
class Reading:
    """A property surfaced as a sensor or binary_sensor entity.

    Attributes:
        property: The :class:`NodeProperty` definition.
        platform: Which HA platform hosts this entity.
        is_enum: True when the property's editor carries an enum ``names``
            map — caller should set HA ``device_class="enum"`` and supply
            ``options=[...]`` from the editor.
    """

    property: NodeProperty
    platform: ReadingPlatform
    is_enum: bool = False


class AuxPlatform(StrEnum):
    """Candidate HA platform for one coalesced aux control.

    A *candidate* — the consumer keeps final say (type-tier-1,
    bespoke/service routing, HA-device grouping sit on top).
    """

    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"
    NUMBER = "number"
    SELECT = "select"
    SWITCH = "switch"
    BUTTON = "button"


@dataclass(slots=True)
class AuxControl:
    """One logical aux control: a read status and/or a write command, coalesced.

    The IoX nodedef expresses the read/write pairing via a command
    parameter's ``init`` (the id of the ``<st>`` status it is
    "initialized and synchronized with" — e.g. a heat-setpoint
    command's param ``init="CLISPH"``; an Insteon i3 flags sub-node's
    ``GV0`` param ``init="ST"`` ⇄ the ``ST`` "Mode" status). This is
    the authoritative pairing key — **not** naive id matching: the cmd
    id and the status id can differ (i3 ``GV0`` ⇄ ``ST``).

    Controllable-owned ids and ``QUERY`` are already excluded (the
    controllable platform represents those). ``ST`` is *not* special
    here: it is removed only when a controllable platform owns it;
    otherwise it is an ordinary coalescable status.

    Attributes:
        id: The control id (the status id when read/write paired, else
            the command id, else the property id).
        readable: A backing status property exists (readback source).
        writable: An accept command drives it.
        candidate_platform: Editor-shape-derived HA platform candidate,
            or ``None`` when no editor resolves (consumer falls back).
        property: The backing :class:`NodeProperty`, if readable.
        command: The driving :class:`Command`, if writable.
        editor_id: The editor governing the control — the write
            command's parameter editor when writable (the write
            affordance is authoritative), else the property editor.
        is_enum: The governing editor carries an enum ``names`` map.
    """

    id: str
    readable: bool
    writable: bool
    candidate_platform: AuxPlatform | None = None
    property: NodeProperty | None = None
    command: Command | None = None
    editor_id: str | None = None
    is_enum: bool = False


@dataclass(slots=True)
class ClassificationResult:
    """The set of HA platform contributions for one nodedef.

    Attributes:
        controllable: The controllable platform, or ``None`` for a
            read-only / event-only node.
        controllable_command_ids: Command ids that belong to the
            controllable platform (so they aren't double-counted as
            buttons). Empty when ``controllable`` is ``None``.
        triggers: Commands the node emits — surface as ``device_trigger``.
        buttons: Accept commands pressable with zero args (parameterless,
            or all parameters ``optional``) — one fire-and-forget
            ``button`` entity each. Excludes ``QUERY`` and
            controllable-claimed cmds.
        parameterized_commands: Accept commands with at least one required
            parameter. Not plain buttons; left for consumers that map
            parameter editors to input entities. Same QUERY / controllable
            exclusions as ``buttons``.
        readings: Per-property reading entities.
        aux_controls: Coalesced read/write controls (see the module
            docstring) — the unified successor to ``readings`` /
            ``parameterized_commands`` / ``buttons``.
    """

    controllable: ControllablePlatform | None = None
    controllable_command_ids: frozenset[str] = field(default_factory=frozenset)
    triggers: list[Command] = field(default_factory=list)
    buttons: list[Command] = field(default_factory=list)
    parameterized_commands: list[Command] = field(default_factory=list)
    readings: list[Reading] = field(default_factory=list)
    aux_controls: list[AuxControl] = field(default_factory=list)


#: ``QUERY`` is implicitly accepted by every node — never a "button".
_QUERY_CMDS = frozenset({CMD_QUERY})

_LIGHT_DIMMER_HINTS = frozenset({CMD_BRIGHTEN, CMD_DIM, CMD_FADE_UP, CMD_FADE_DOWN, CMD_FADE_STOP})
#: Commands the light/switch controllable platform actually maps onto
#: its on/off surface. ``DFON``/``DFOF`` ("fast on/off") and the
#: momentary paddle-simulation verbs (``BRT``/``DIM``/``FDUP``/
#: ``FDDOWN``/``FDSTOP``) deliberately stay *out* of this set — they
#: have no HA light/switch equivalent, so the classifier lets them
#: fall through to ``buttons`` (one HA ``button`` entity each) rather
#: than absorbing and hiding them. They still feed dimmer *detection*
#: via :data:`_LIGHT_DIMMER_HINTS`.
_LIGHT_SWITCH_CMDS = frozenset({CMD_ON, CMD_OFF})
#: On a thermostat ``BRT``/``DIM`` mean setpoint up/down, not light
#: dimming. ``CLISPC``/``CLISPH`` are reported as properties *and*
#: accepted as setpoint commands (IoX dual-purposes the id), as are
#: ``CLIMD``/``CLIFS``.
_THERMOSTAT_CMDS = frozenset(
    {PROP_SETPOINT_COOL, PROP_SETPOINT_HEAT, CMD_CLIMATE_MODE, CMD_CLIMATE_FAN_SETTING, CMD_BRIGHTEN, CMD_DIM}
)
_LOCK_CMDS = frozenset({CMD_SECURE})
_ALARM_CMDS = frozenset({CMD_ALARM_ARM, CMD_ALARM_DISARM})
_COVER_CMDS = frozenset({CMD_FADE_UP, CMD_FADE_DOWN, CMD_FADE_STOP})

_LIGHT_STATE_PROPS = frozenset({PROP_STATUS, PROP_ON_LEVEL, PROP_RAMP_RATE})
#: Properties the climate entity owns (so they aren't surfaced as
#: separate sensors). Includes the ``CLIMD``/``CLIFS`` ids that the
#: thermostat both reports and accepts (see ``_THERMOSTAT_CMDS``).
_THERMOSTAT_STATE_PROPS = frozenset(
    {
        PROP_STATUS,
        PROP_SETPOINT_COOL,
        PROP_SETPOINT_HEAT,
        PROP_SETPOINT_COOL_DELTA,
        PROP_SETPOINT_HEAT_DELTA,
        CMD_CLIMATE_MODE,
        CMD_CLIMATE_FAN_SETTING,
        PROP_TEMPERATURE,
        PROP_HEAT_COOL_STATE,
        PROP_SCHEDULE_MODE,
    }
)
_LOCK_STATE_PROPS = frozenset({PROP_STATUS})
_COVER_STATE_PROPS = frozenset({PROP_STATUS, PROP_ON_LEVEL})
_ALARM_STATE_PROPS = frozenset({PROP_STATUS})

#: UOM ids whose property values are binary (two-state) — surface as
#: HA ``binary_sensor`` entities. The base case is UOM 2 (true/false);
#: UOM 78 ("On/Off where Off=0, On=100") and UOM 79 ("Open/Closed
#: where Open=0, Closed=100") are also two-state in practice and
#: belong here even though their value range is wider than 0/1.
_BINARY_UOMS = frozenset({UOM_BOOLEAN, UOM_ON_OFF, UOM_OPEN_CLOSED})
#: Always-numeric UOMs: percent (0-100) and the 8-bit byte range
#: (0-255). NUMBER even when the profile spells the span as a wide
#: ``subset`` rather than ``min``/``max``.
_NUMERIC_UOMS = frozenset({UOM_PERCENTAGE, UOM_BYTE})
#: Generic editor ids that name a value *shape* regardless of UOM (PG3
#: plugin nodedefs lean on these; firmware nodedefs carry ``I_*`` /
#: ``ZW_*`` ids whose shape is read off the range instead).
_NUMERIC_EDITOR_IDS = frozenset({"INTEGER", "FLOAT"})
_BOOL_EDITOR_IDS = frozenset({"BOOL", "bool", "I_BOOL"})


EditorResolver = Callable[[str], Editor | None]


def _detect_controllable(
    accept_ids: frozenset[str],
    properties: dict[str, NodeProperty],
    on_takes_level: bool,
) -> tuple[ControllablePlatform | None, frozenset[str]]:
    """Pick the single controllable platform plus the commands that belong to it.

    Order matters: thermostat is checked before light/switch because the
    Insteon Thermostat nodedef accepts ``BRT``/``DIM`` (interpreted as
    setpoint up/down on a thermostat, not as a light dimmer).

    ``on_takes_level`` is True when the accepted ``DON`` declares at
    least one parameter (the on-level). A node is only a *dimmer* —
    LIGHT rather than SWITCH — if it can actually be commanded to a
    level via ``DON``. Some node-server nodedefs (and the legacy
    ``X10`` nodedef) carry on-level properties or ``BRT``/``DIM`` hints
    but a *parameterless* ``DON``; HA's light platform drives
    brightness with ``DON <level>``, so without the param the slider
    just fails. Those degrade to SWITCH (their level-set verbs still
    surface as ``parameterized_commands`` / ``buttons``).
    """
    has_dim_or_switch = CMD_ON in accept_ids and CMD_OFF in accept_ids
    has_thermostat_setpoint = PROP_SETPOINT_COOL in accept_ids or PROP_SETPOINT_HEAT in accept_ids
    has_lock = CMD_SECURE in accept_ids
    has_alarm = bool(_ALARM_CMDS & accept_ids)
    has_cover_only = (_COVER_CMDS & accept_ids) and not has_dim_or_switch
    has_on_level = PROP_ON_LEVEL in properties

    if has_lock:
        return ControllablePlatform.LOCK, _LOCK_CMDS & accept_ids
    if has_thermostat_setpoint:
        return ControllablePlatform.CLIMATE, _THERMOSTAT_CMDS & accept_ids
    if has_alarm:
        return ControllablePlatform.ALARM_CONTROL_PANEL, _ALARM_CMDS & accept_ids
    if has_cover_only:
        return ControllablePlatform.COVER, _COVER_CMDS & accept_ids
    if has_dim_or_switch:
        is_dimmer = (has_on_level or bool(_LIGHT_DIMMER_HINTS & accept_ids)) and on_takes_level
        platform = ControllablePlatform.LIGHT if is_dimmer else ControllablePlatform.SWITCH
        return platform, _LIGHT_SWITCH_CMDS & accept_ids
    return None, frozenset()


def _controllable_owned_prop_ids(controllable: ControllablePlatform | None) -> frozenset[str]:
    """Property ids the controllable platform represents itself (so they
    don't surface as separate readings / aux controls)."""
    if controllable in (ControllablePlatform.LIGHT, ControllablePlatform.SWITCH):
        return _LIGHT_STATE_PROPS
    if controllable is ControllablePlatform.CLIMATE:
        return _THERMOSTAT_STATE_PROPS
    if controllable is ControllablePlatform.LOCK:
        return _LOCK_STATE_PROPS
    if controllable is ControllablePlatform.COVER:
        return _COVER_STATE_PROPS
    if controllable is ControllablePlatform.ALARM_CONTROL_PANEL:
        return _ALARM_STATE_PROPS
    return frozenset()


def _filter_state_properties(
    controllable: ControllablePlatform | None, properties: dict[str, NodeProperty]
) -> list[NodeProperty]:
    """Drop properties already represented by the controllable platform."""
    skip = _controllable_owned_prop_ids(controllable)
    return [p for p in properties.values() if p.id not in skip]


def _classify_property(prop: NodeProperty, find_editor: EditorResolver | None) -> Reading:
    """Decide whether a property is a sensor or binary_sensor and detect enum-ness.

    Editors *can* carry multiple ranges (e.g. an editor that supports
    both °F and °C), but the classifier deliberately reads ``ranges[0]``
    only — at this point we have no live property value (so no UOM
    hint to disambiguate) and the controller's own first-range pick
    is the closest thing to a default. Consumers who need to render
    multi-UOM data switch on the live property's ``uom`` and resolve
    the matching range themselves at render time.
    """
    platform = ReadingPlatform.SENSOR
    is_enum = False
    if find_editor is not None:
        editor = find_editor(prop.editor_id)
        if editor is not None and editor.ranges:
            rng = editor.ranges[0]
            if rng.uom in _BINARY_UOMS:
                platform = ReadingPlatform.BINARY_SENSOR
            if rng.names:
                is_enum = True
    return Reading(property=prop, platform=platform, is_enum=is_enum)


def _aux_write_platform(editor: Editor | None) -> AuxPlatform | None:
    """Editor shape → candidate platform for a *writable* aux control.

    Layered, cheapest signal first — intentionally mirrors the
    consumer's historical ``platform_for_control`` so the advisory
    candidate matches what the consumer already does. Note ``names``
    *and* numeric bounds together resolves to NUMBER, not SELECT (an
    enum with a declared range — unusual but legal; matches the
    consumer). ``None`` when nothing resolves — the consumer falls back.
    """
    if editor is None or not editor.ranges:
        return None
    if editor.id in _NUMERIC_EDITOR_IDS:
        return AuxPlatform.NUMBER
    if editor.id in _BOOL_EDITOR_IDS:
        return AuxPlatform.SWITCH
    rng = editor.ranges[0]
    if rng.uom in _BINARY_UOMS:
        return AuxPlatform.SWITCH
    if rng.uom in _NUMERIC_UOMS:
        return AuxPlatform.NUMBER
    if rng.uom == UOM_INDEX:
        return AuxPlatform.SELECT
    has_bounds = rng.min is not None or rng.max is not None
    if rng.names and not has_bounds:
        return AuxPlatform.SELECT
    if rng.subset and not rng.names and not has_bounds:
        return AuxPlatform.SELECT
    if has_bounds:
        return AuxPlatform.NUMBER
    return None


def _aux_from_command(
    cmd: Command, props: dict[str, NodeProperty], find_editor: EditorResolver | None
) -> AuxControl:
    """Build the aux control for one (non-controllable, non-QUERY)
    accept command.

    Button-shaped (sendable with no positional args) → a fire BUTTON.
    Otherwise the parameter's ``init`` names the ``<st>`` it is
    "initialized and synchronized with" — pair with that status (read +
    write) when it exists, else a write-only control keyed by command
    id. Pairing is by ``init``, **not** id matching: the command id and
    status id can differ (Insteon i3 ``GV0`` ``init="ST"`` ⇄ ``ST``).
    """
    params = cmd.parameters
    if all(p.optional for p in params):
        return AuxControl(
            id=cmd.id,
            readable=False,
            writable=True,
            candidate_platform=AuxPlatform.BUTTON,
            command=cmd,
            editor_id=(params[0].editor_id or None) if params else None,
        )
    init_param = next((p for p in params if p.init), params[0])
    editor_id = init_param.editor_id or None
    editor = find_editor(editor_id) if (find_editor and editor_id) else None
    candidate = _aux_write_platform(editor)
    is_enum = bool(editor and editor.ranges and editor.ranges[0].names)
    status_id = init_param.init
    if status_id and status_id in props:
        return AuxControl(
            id=status_id,
            readable=True,
            writable=True,
            candidate_platform=candidate,
            property=props[status_id],
            command=cmd,
            editor_id=editor_id,
            is_enum=is_enum,
        )
    return AuxControl(
        id=cmd.id,
        readable=False,
        writable=True,
        candidate_platform=candidate,
        command=cmd,
        editor_id=editor_id,
        is_enum=is_enum,
    )


def _build_aux_controls(
    nodedef: NodeDef,
    controllable: ControllablePlatform | None,
    controllable_cmd_ids: frozenset[str],
    find_editor: EditorResolver | None,
) -> list[AuxControl]:
    """Coalesce non-controllable status/command pairs into one control
    each. Stable, intentional order (consumers may rely on it): writable
    controls first in ``accepts`` order, then read-only residuals in
    ``properties`` order. Controllable-owned ids and ``QUERY`` are
    already excluded; a paired writer still reads its status back even
    when the property is controllable-filtered from standalone readings
    (e.g. a light's ``OL`` setter)."""
    props = nodedef.properties
    # Two accepts shouldn't legitimately pair to the same control id
    # (UDI won't ship two writers for one status), but guard it: first
    # in ``accepts`` order wins, so a stray duplicate can't emit two
    # controls with the same id.
    cmd_controls: list[AuxControl] = []
    seen: set[str] = set()
    for cmd in nodedef.cmds.accepts:
        if cmd.id in _QUERY_CMDS or cmd.id in controllable_cmd_ids:
            continue
        control = _aux_from_command(cmd, props, find_editor)
        if control.id in seen:
            continue
        seen.add(control.id)
        cmd_controls.append(control)
    # Any id already surfaced by a command-side control owns that id:
    # a same-id property is its readback, not a separate sensor. Keying
    # on every cmd-control id (not just paired status ids) also blocks a
    # write-only command (e.g. a no-``init`` ``BL`` setter) from
    # colliding with an unrelated same-id property. No real UDI nodedef
    # hits this today — invariant guard, not a live fix.
    consumed = {c.id for c in cmd_controls}

    read_controls: list[AuxControl] = []
    for prop in _filter_state_properties(controllable, props):
        if prop.id in consumed:
            continue
        reading = _classify_property(prop, find_editor)
        read_controls.append(
            AuxControl(
                id=prop.id,
                readable=True,
                writable=False,
                candidate_platform=(
                    AuxPlatform.BINARY_SENSOR
                    if reading.platform is ReadingPlatform.BINARY_SENSOR
                    else AuxPlatform.SENSOR
                ),
                property=prop,
                editor_id=prop.editor_id or None,
                is_enum=reading.is_enum,
            )
        )
    return cmd_controls + read_controls


def classify(nodedef: NodeDef, find_editor: EditorResolver | None = None) -> ClassificationResult:
    """Classify a nodedef into HA platform contributions.

    Args:
        nodedef: The nodedef to classify. Same shape regardless of native vs
            PG3 plugin origin.
        find_editor: Optional editor resolver, scoped to ``nodedef``'s
            family/instance. When provided, property readings are split into
            sensor vs binary_sensor by editor UOM and tagged ``is_enum`` for
            enum editors. When ``None`` (e.g. in unit tests), all readings
            default to ``sensor`` with ``is_enum=False`` — callers can still
            render them, just without device-class hints.

    Returns:
        A :class:`ClassificationResult` with controllable / triggers /
        buttons / parameterized_commands / readings / aux_controls
        populated. ``find_editor`` also drives ``aux_controls``
        candidate platforms; without it writable controls fall back to
        ``candidate_platform=None`` for the consumer to resolve.
    """
    accept_ids = frozenset(c.id for c in nodedef.cmds.accepts)
    on_cmd = next((c for c in nodedef.cmds.accepts if c.id == CMD_ON), None)
    on_takes_level = on_cmd is not None and len(on_cmd.parameters) > 0
    controllable, controllable_cmd_ids = _detect_controllable(accept_ids, nodedef.properties, on_takes_level)

    triggers = list(nodedef.cmds.sends)

    candidate_cmds = [
        c for c in nodedef.cmds.accepts if c.id not in _QUERY_CMDS and c.id not in controllable_cmd_ids
    ]
    # Button-shaped = sendable with no positional args: parameterless or
    # every param optional (controller defaults — e.g. Insteon BEEP).
    buttons = [c for c in candidate_cmds if all(p.optional for p in c.parameters)]
    parameterized_commands = [c for c in candidate_cmds if not all(p.optional for p in c.parameters)]

    readings = [
        _classify_property(prop, find_editor)
        for prop in _filter_state_properties(controllable, nodedef.properties)
    ]

    aux_controls = _build_aux_controls(nodedef, controllable, controllable_cmd_ids, find_editor)

    return ClassificationResult(
        controllable=controllable,
        controllable_command_ids=controllable_cmd_ids,
        triggers=triggers,
        buttons=buttons,
        parameterized_commands=parameterized_commands,
        readings=readings,
        aux_controls=aux_controls,
    )
