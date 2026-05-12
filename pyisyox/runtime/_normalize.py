"""UOM normalization for live node-property values.

Some IoX devices report a property in a different unit-of-measure than
its nodedef *editor* declares. The classic case: an Insteon dimmer
reports ``OL`` (and ``ST``) as a **UOM-100 0-255 byte** (``191`` for
"75%"), while the ``I_OL`` editor — the one the ``/rest/nodes/.../cmd``
write surface uses — describes the **UOM-51 0-100% slider**. Z-Wave /
Zigbee / Matter dimmers may report either form depending on firmware.

This module converts a reported :class:`~pyisyox.client.NodePropertyValue`
into the editor's canonical UOM so consumers see one consistent shape
regardless of which firmware or transport produced it (REST load vs. a
WebSocket ``<action>`` frame). When the reported UOM already matches one
of the editor's ranges — the common case — the value passes through
untouched.

The conversion set is intentionally tiny; only genuinely mismatched
pairs belong here.
"""

from __future__ import annotations

from collections.abc import Callable

from pyisyox.client import NodePropertyValue
from pyisyox.schema.editor import Editor


def _byte_to_percent(displayed: float) -> float:
    """0-255 byte → 0-100 percent (Insteon dimmer convention).

    ``round(191 * 100 / 255) == 75``; ``153 → 60``; ``255 → 100``.
    Matches the ``fmtAct`` string the controller computes for the byte.
    """
    return float(round(displayed * 100 / 255))


#: ``(reported_uom, editor_uom) -> (transform, target_precision)``. The
#: transform receives the *displayed* value (raw already divided by
#: ``10**precision``) and returns the displayed value in the target UOM.
_CONVERSIONS: dict[tuple[str, str], tuple[Callable[[float], float], int]] = {
    ("100", "51"): (_byte_to_percent, 0),
}


def normalize_property_value(prop: NodePropertyValue, editor: Editor | None) -> NodePropertyValue:
    """Return ``prop`` re-expressed in its editor's canonical UOM.

    Passes ``prop`` through unchanged when there's no editor, no UOM on
    the value, the UOM already matches one of the editor's ranges, no
    conversion is defined for the ``(reported, editor)`` UOM pair, or the
    value isn't numeric.
    """
    if editor is None or not prop.uom:
        return prop
    editor_uoms = {r.uom for r in editor.ranges}
    if not editor_uoms or prop.uom in editor_uoms:
        return prop
    for target_uom in editor_uoms:
        conv = _CONVERSIONS.get((prop.uom, target_uom))
        if conv is None:
            continue
        transform, target_prec = conv
        try:
            raw = float(prop.value)
        except (TypeError, ValueError):
            return prop
        displayed = raw / (10**prop.precision) if prop.precision else raw
        new_value = transform(displayed)
        text = str(int(new_value)) if float(new_value).is_integer() else f"{new_value}"
        return NodePropertyValue(
            id=prop.id,
            value=text,
            formatted=prop.formatted,
            uom=target_uom,
            name=prop.name,
            precision=target_prec,
        )
    return prop
