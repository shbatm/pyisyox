"""Normalization for live node-property values.

Two transforms, applied on every ``node.properties`` / ``node.status``
read so consumers see one consistent shape regardless of firmware or
transport (REST load vs. a WebSocket ``<action>`` frame):

1. **Precision decode.** IoX reports a value as an integer plus a
   ``prec`` decimal shift — ``value="954" prec="1"`` is ``95.4`` (the
   controller's own ``formatted`` confirms: ``"95.4°F"``). This is the
   read-side counterpart of the send contract (the controller scales
   device-side; see :mod:`pyisyox.schema.editor`): the displayed value
   is ``raw / 10**prec``, and the returned :class:`NodePropertyValue`
   carries that with ``precision=0`` so consumers never re-shift it.
   Independent of the editor — the wire frame is self-describing.

2. **UOM canonicalization.** Some devices report a property in a
   different unit than its nodedef *editor* declares — the classic
   case: an Insteon dimmer reports ``OL``/``ST`` as a **UOM-100 0-255
   byte** (``191`` for "75%") while the ``I_OL`` editor (and the
   ``/cmd`` write surface) speak the **UOM-51 0-100% slider**. The
   conversion set is intentionally tiny; only genuinely mismatched
   pairs belong here. When the reported UOM already matches one of the
   editor's ranges — the common case — only step 1 applies.

(UOM-101 / "degrees" half-degree raw is *not* handled here — that legacy
Insteon-thermostat encoding stays a consumer concern; there's no such
hardware in any capture to verify against.)
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
#: transform receives the *displayed* value (already precision-decoded)
#: and returns the displayed value in the target UOM. ``target_precision``
#: **must be 0** — the post-condition of ``normalize_property_value`` is
#: ``precision == 0`` (the value is fully decoded; consumers never
#: re-shift). A non-zero entry would violate that invariant.
_CONVERSIONS: dict[tuple[str, str], tuple[Callable[[float], float], int]] = {
    ("100", "51"): (_byte_to_percent, 0),
}


def _num_text(value: float, prec: int) -> str:
    """Stringify to ``prec`` decimals; an integral value stays ``"75"``
    not ``"75.0"``. Fixed-point (``f"{value:.{prec}f}"``) — never
    ``f"{value}"``, which flips to scientific notation below ~1e-4
    (a ``prec=5`` reading would otherwise wire ``"1e-05"``)."""
    return str(int(value)) if value.is_integer() else f"{value:.{prec}f}"


def _decode_precision(prop: NodePropertyValue) -> NodePropertyValue:
    """Apply the reported ``prec`` decimal shift; ``precision`` → 0.

    Passthrough when ``prec`` is 0 (nothing to shift) or the value isn't
    numeric (plugin nodes legitimately report non-numeric readings —
    consumers fall back to ``formatted``).
    """
    if not prop.precision:
        return prop
    try:
        raw = float(prop.value)
    except (TypeError, ValueError):
        return prop
    displayed = round(raw / (10**prop.precision), prop.precision)
    return NodePropertyValue(
        id=prop.id,
        value=_num_text(displayed, prop.precision),
        formatted=prop.formatted,
        uom=prop.uom,
        name=prop.name,
        precision=0,
    )


def normalize_property_value(prop: NodePropertyValue, editor: Editor | None) -> NodePropertyValue:
    """Return ``prop`` precision-decoded and re-expressed in its editor's
    canonical UOM.

    Step 1 (precision) always runs — it's editor-independent. Step 2
    (UOM conversion) passes through unchanged when there's no editor, no
    UOM, the UOM already matches one of the editor's ranges, no
    conversion is defined for the ``(reported, editor)`` UOM pair, or
    the value isn't numeric.
    """
    prop = _decode_precision(prop)
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
        # ``prop`` is already precision-decoded (precision == 0); the
        # transform output is in displayed units, ``target_prec`` is 0.
        new_value = transform(raw)
        return NodePropertyValue(
            id=prop.id,
            value=_num_text(new_value, target_prec),
            formatted=prop.formatted,
            uom=target_uom,
            name=prop.name,
            precision=target_prec,
        )
    return prop
