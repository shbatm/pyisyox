"""Tests for :func:`pyisyox.runtime._normalize.normalize_property_value`."""

from __future__ import annotations

import pytest

from pyisyox.client import NodePropertyValue
from pyisyox.runtime._normalize import normalize_property_value
from pyisyox.schema.editor import Editor, EditorRange


def _editor(*ranges: EditorRange) -> Editor:
    return Editor(id="X", ranges=list(ranges))


PCT = EditorRange(uom="51", min=0.0, max=100.0)
BYTE = EditorRange(uom="100", min=0.0, max=255.0)


@pytest.mark.parametrize(
    ("raw", "expected_value"),
    [("255", "100"), ("191", "75"), ("153", "60"), ("0", "0"), ("128", "50")],
)
def test_byte_reported_against_percent_editor_is_scaled(raw: str, expected_value: str) -> None:
    prop = NodePropertyValue(id="OL", value=raw, formatted=f"{expected_value}%", uom="100", precision=0)
    out = normalize_property_value(prop, _editor(PCT))
    assert out.value == expected_value
    assert out.uom == "51"
    assert out.precision == 0
    assert out.formatted == f"{expected_value}%"  # display string preserved verbatim


def test_matching_uom_passes_through_unchanged() -> None:
    prop = NodePropertyValue(id="OL", value="75", uom="51", precision=0)
    assert normalize_property_value(prop, _editor(PCT)) is prop


def test_uom_in_one_of_multiple_ranges_passes_through() -> None:
    """A dual-range editor that *declares* the reported UOM (e.g. ZB_OL's
    ``[{uom:51},{uom:100}]``) needs no conversion — the byte form is one
    of its own ranges."""
    prop = NodePropertyValue(id="OL", value="191", uom="100", precision=0)
    assert normalize_property_value(prop, _editor(PCT, BYTE)) is prop


def test_no_editor_passes_through() -> None:
    prop = NodePropertyValue(id="GV1", value="191", uom="100")
    assert normalize_property_value(prop, None) is prop


def test_unknown_conversion_pair_passes_through() -> None:
    """Reported UOM differs from the editor's, but no conversion is
    defined for the pair — leave it alone rather than guess."""
    prop = NodePropertyValue(id="ST", value="42", uom="56", precision=0)
    assert normalize_property_value(prop, _editor(PCT)) is prop


def test_non_numeric_value_passes_through() -> None:
    prop = NodePropertyValue(id="ST", value="", uom="100", precision=0)
    assert normalize_property_value(prop, _editor(PCT)) is prop


# --- precision decode (read-side counterpart of the send contract) -------

TEMP = EditorRange(uom="17", min=-30.0, max=130.0, precision=1)


def test_reported_precision_is_decoded_when_uom_matches() -> None:
    """``value="954" prec=1`` (a PG3 ``virtualtemp`` reading) decodes to
    ``95.4`` with ``precision=0``; the UOM already matches the editor so
    only the precision step applies. ``formatted`` is preserved."""
    prop = NodePropertyValue(
        id="GV1", value="954", formatted="95.4°F", uom="17", precision=1
    )
    out = normalize_property_value(prop, _editor(TEMP))
    assert out.value == "95.4"
    assert out.precision == 0
    assert out.uom == "17"
    assert out.formatted == "95.4°F"


def test_reported_precision_decoded_without_editor() -> None:
    """Precision decode is editor-independent (the wire frame is
    self-describing) — works for dynamically-provisioned nodes with no
    nodedef/editor."""
    prop = NodePropertyValue(id="GV2", value="11114", uom="69", precision=4)
    out = normalize_property_value(prop, None)
    assert out.value == "1.1114"
    assert out.precision == 0


def test_precision_decode_integral_result_has_no_dot() -> None:
    """``950`` prec 1 → ``95`` (not ``95.0``) so the URL/state stays clean."""
    prop = NodePropertyValue(id="GV3", value="950", uom="17", precision=1)
    out = normalize_property_value(prop, _editor(TEMP))
    assert out.value == "95"
    assert out.precision == 0


def test_precision_decode_then_uom_conversion_compose() -> None:
    """Both steps apply: a byte reported with a prec shift is first
    precision-decoded, then byte→percent canonicalised."""
    prop = NodePropertyValue(id="OL", value="1910", uom="100", precision=1)
    out = normalize_property_value(prop, _editor(PCT))
    # 1910 / 10**1 = 191 (byte) → round(191*100/255) = 75 %
    assert out.value == "75"
    assert out.uom == "51"
    assert out.precision == 0


def test_non_numeric_with_precision_passes_through() -> None:
    prop = NodePropertyValue(id="ST", value="n/a", uom="17", precision=1)
    assert normalize_property_value(prop, _editor(TEMP)) is prop


def test_non_numeric_precision_and_mismatched_uom_passes_through() -> None:
    """Non-numeric + prec>0 + a UOM that *would* hit the conversion loop:
    ``_decode_precision`` returns the original (non-numeric), then the
    UOM step also fails to ``float()`` it and returns it unchanged."""
    prop = NodePropertyValue(id="OL", value="n/a", uom="100", precision=1)
    assert normalize_property_value(prop, _editor(PCT)) is prop
