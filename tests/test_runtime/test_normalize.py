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
