"""Tests for the unit-of-measure table.

After absorbing the legacy ``UOM_FRIENDLY_NAME`` dict from
``constants.py``, the schema's ``PREDEFINED_UOMS`` is the single
canonical source for UOM display names + descriptions + categories.
These tests lock in the table size and the friendly-name parity so a
future refactor doesn't accidentally drop entries.
"""

from __future__ import annotations

import dataclasses

import pytest

from pyisyox.schema.uom import PREDEFINED_UOMS, UNKNOWN_UOM, UOMEntry, get_uom


def test_table_covers_full_udi_range() -> None:
    """UDI publishes UOMs 0-154 (gaps for reserved ids). Verify the table
    has at least 150 entries; short of that, the merge regressed."""
    assert len(PREDEFINED_UOMS) >= 150


def test_unknown_uom_sentinel_is_zero() -> None:
    assert UNKNOWN_UOM == "0"
    entry = PREDEFINED_UOMS[UNKNOWN_UOM]
    assert entry.id == "0"
    assert entry.name == ""
    assert entry.description.lower().startswith("the unit of measure is unknown")


def test_get_uom_returns_unknown_for_missing_id() -> None:
    assert get_uom("999") is PREDEFINED_UOMS[UNKNOWN_UOM]
    assert get_uom("") is PREDEFINED_UOMS[UNKNOWN_UOM]


def test_common_uoms_have_short_symbol_names() -> None:
    """HA's ``unit_of_measurement`` wants short symbols, not long
    descriptions. Verify the merge picked the symbol form for the
    everyday units."""
    assert PREDEFINED_UOMS["1"].name == "A"  # Amps
    assert PREDEFINED_UOMS["4"].name == "°C"
    assert PREDEFINED_UOMS["17"].name == "°F"
    assert PREDEFINED_UOMS["33"].name == "kWh"
    assert PREDEFINED_UOMS["51"].name == "%"
    assert PREDEFINED_UOMS["72"].name == "V"
    assert PREDEFINED_UOMS["73"].name == "W"


def test_categories_present_for_classifier_consumers() -> None:
    """The HA platform classifier reads ``category_id`` to decide between
    sensor device classes. A drift here breaks plugin-node routing."""
    assert PREDEFINED_UOMS["1"].category_id == "electric_current"
    assert PREDEFINED_UOMS["4"].category_id == "temperature"
    assert PREDEFINED_UOMS["17"].category_id == "temperature"
    assert PREDEFINED_UOMS["33"].category_id == "energy"
    assert PREDEFINED_UOMS["69"].category_id == "volume"
    assert PREDEFINED_UOMS["73"].category_id == "power"


def test_uom_2_boolean_has_no_unit_string() -> None:
    """UOM 2 is ``boolean`` — surfaces as binary_sensor, no unit suffix."""
    entry = PREDEFINED_UOMS["2"]
    assert entry.name == ""


def test_uom_entry_is_frozen() -> None:
    """UOMEntry is a frozen dataclass; consumers can safely cache instances
    without worrying about mutation."""
    entry = PREDEFINED_UOMS["1"]
    assert dataclasses.is_dataclass(entry)
    assert isinstance(entry, UOMEntry)
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.name = "mutated"  # type: ignore[misc]
