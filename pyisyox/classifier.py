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

One HA *device* per node aggregates entities from all five buckets.
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
    UOM_ON_OFF,
    UOM_OPEN_CLOSED,
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
    """

    controllable: ControllablePlatform | None = None
    controllable_command_ids: frozenset[str] = field(default_factory=frozenset)
    triggers: list[Command] = field(default_factory=list)
    buttons: list[Command] = field(default_factory=list)
    parameterized_commands: list[Command] = field(default_factory=list)
    readings: list[Reading] = field(default_factory=list)


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


EditorResolver = Callable[[str], Editor | None]


def _detect_controllable(
    accept_ids: frozenset[str], properties: dict[str, NodeProperty]
) -> tuple[ControllablePlatform | None, frozenset[str]]:
    """Pick the single controllable platform plus the commands that belong to it.

    Order matters: thermostat is checked before light/switch because the
    Insteon Thermostat nodedef accepts ``BRT``/``DIM`` (interpreted as
    setpoint up/down on a thermostat, not as a light dimmer).
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
        is_dimmer = has_on_level or bool(_LIGHT_DIMMER_HINTS & accept_ids)
        platform = ControllablePlatform.LIGHT if is_dimmer else ControllablePlatform.SWITCH
        return platform, _LIGHT_SWITCH_CMDS & accept_ids
    return None, frozenset()


def _filter_state_properties(
    controllable: ControllablePlatform | None, properties: dict[str, NodeProperty]
) -> list[NodeProperty]:
    """Drop properties already represented by the controllable platform."""
    if controllable is None:
        return list(properties.values())
    if controllable in (ControllablePlatform.LIGHT, ControllablePlatform.SWITCH):
        skip = _LIGHT_STATE_PROPS
    elif controllable is ControllablePlatform.CLIMATE:
        skip = _THERMOSTAT_STATE_PROPS
    elif controllable is ControllablePlatform.LOCK:
        skip = _LOCK_STATE_PROPS
    elif controllable is ControllablePlatform.COVER:
        skip = _COVER_STATE_PROPS
    elif controllable is ControllablePlatform.ALARM_CONTROL_PANEL:
        skip = _ALARM_STATE_PROPS
    else:
        skip = frozenset()
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
        buttons / parameterized_commands / readings populated.
    """
    accept_ids = frozenset(c.id for c in nodedef.cmds.accepts)
    controllable, controllable_cmd_ids = _detect_controllable(accept_ids, nodedef.properties)

    triggers = list(nodedef.cmds.sends)

    candidate_cmds = [
        c for c in nodedef.cmds.accepts if c.id not in _QUERY_CMDS and c.id not in controllable_cmd_ids
    ]
    # A command is button-shaped when it can be sent with no positional
    # args: parameterless, or every parameter ``optional`` (controller
    # applies defaults — e.g. Insteon BEEP's optional ``level``).
    # ``all([])`` is True, so parameterless commands fall through here.
    buttons = [c for c in candidate_cmds if all(p.optional for p in c.parameters)]
    parameterized_commands = [c for c in candidate_cmds if not all(p.optional for p in c.parameters)]

    readings = [
        _classify_property(prop, find_editor)
        for prop in _filter_state_properties(controllable, nodedef.properties)
    ]

    return ClassificationResult(
        controllable=controllable,
        controllable_command_ids=controllable_cmd_ids,
        triggers=triggers,
        buttons=buttons,
        parameterized_commands=parameterized_commands,
        readings=readings,
    )
