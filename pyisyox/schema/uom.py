"""Unit-of-measure table for IoX 6.

Each editor range carries a ``uom`` id (string) that resolves into this
table. ``category_id`` is a coarse semantic grouping (temperature, volume,
electric_current, etc.) used by HA's platform classifier to decide between
``sensor`` device classes.

This is a partial port of UDI's nucore-ai ``uom.py`` covering every UOM
seen in capture against a real eisy plus the most common categories.
The full UDI table has ~120 entries and is additive — extend here as new
UOMs surface.

Source: ``/rest/profiles`` editor ``ranges[].uom`` references; UDI nucore-ai
``src/nucore/uom.py``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UOMEntry:
    """A unit-of-measure entry.

    Attributes:
        id: UOM identifier as it appears in profile responses.
        name: Short display name (suitable for sensor unit_of_measurement).
        description: Free-text description.
        category_id: Coarse semantic category (see module docstring).
    """

    id: str
    name: str
    description: str = ""
    category_id: str | None = None


UNKNOWN_UOM = "0"


PREDEFINED_UOMS: dict[str, UOMEntry] = {
    "0": UOMEntry("0", "", "Unknown / unitless"),
    "1": UOMEntry("1", "A", "Electric current in amperes", "electric_current"),
    "2": UOMEntry("2", "", "Boolean (0=False, 1=True)", "boolean"),
    "3": UOMEntry("3", "btu/h", "BTU per hour", "power"),
    "4": UOMEntry("4", "°C", "Degrees Celsius", "temperature"),
    "5": UOMEntry("5", "cm", "Centimeters", "distance"),
    "6": UOMEntry("6", "ft³", "Cubic feet", "volume"),
    "7": UOMEntry("7", "ft³/min", "Cubic feet per minute", "volume_flow"),
    "8": UOMEntry("8", "m³", "Cubic meters", "volume"),
    "12": UOMEntry("12", "dB", "Decibels", "sound"),
    "14": UOMEntry("14", "°", "Degrees of arc", "angle"),
    "17": UOMEntry("17", "°F", "Degrees Fahrenheit", "temperature"),
    "23": UOMEntry("23", "kPa", "Kilopascals", "pressure"),
    "24": UOMEntry("24", "in/h", "Inches per hour", "speed"),
    "25": UOMEntry("25", "", "Index / enumerated value", "enum"),
    "33": UOMEntry("33", "kWh", "Kilowatt-hours", "energy"),
    "44": UOMEntry("44", "min", "Minutes", "time_duration"),
    "51": UOMEntry("51", "%", "Percent", "ratio"),
    "56": UOMEntry("56", "", "Raw 1-byte unsigned integer", "raw"),
    "57": UOMEntry("57", "s", "Seconds", "time_duration"),
    "67": UOMEntry("67", "", "Z-Wave thermostat mode index", "enum"),
    "69": UOMEntry("69", "gal", "US gallons", "volume"),
    "71": UOMEntry("71", "UV index", "UV index", "uv_index"),
    "72": UOMEntry("72", "V", "Volts", "voltage"),
    "73": UOMEntry("73", "W", "Watts", "power"),
    "78": UOMEntry("78", "", "On/Off (0=Off, 100=On)", "binary_level"),
    "98": UOMEntry("98", "", "Insteon thermostat mode index", "enum"),
    "99": UOMEntry("99", "", "Thermostat fan mode index", "enum"),
    "100": UOMEntry("100", "", "Raw byte 0-255 (Insteon level)", "raw"),
    "101": UOMEntry("101", "°", "Half-degree temperature offset", "temperature"),
}


def get_uom(uom_id: str) -> UOMEntry:
    """Look up a UOM by id; returns the unknown entry if unrecognised."""
    return PREDEFINED_UOMS.get(uom_id, PREDEFINED_UOMS[UNKNOWN_UOM])
