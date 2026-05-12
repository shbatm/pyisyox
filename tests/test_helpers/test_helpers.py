"""Tests for :mod:`pyisyox.helpers` — datetime parsing + raw-value
conversion utilities. Both replaced runtime deps (``python-dateutil``)
under the rewrite and need their fallbacks pinned."""

from __future__ import annotations

import datetime as dt
import math

import pytest

from pyisyox.constants import (
    EMPTY_TIME,
    ISY_VALUE_UNKNOWN,
    UOM_DOUBLE_TEMP,
    UOM_ISYV4_DEGREES,
)
from pyisyox.helpers import convert_isy_raw_value, parse_isy_datetime

# --- parse_isy_datetime --------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026/05/08 14:30:00", dt.datetime(2026, 5, 8, 14, 30, 0)),  # MILITARY_TIME
        ("2026/05/08 02:30:00 PM", dt.datetime(2026, 5, 8, 14, 30, 0)),  # STANDARD_TIME
        ("20260508 14:30:00", dt.datetime(2026, 5, 8, 14, 30, 0)),  # XML_STRPTIME
        ("260508 14:30:00", dt.datetime(2026, 5, 8, 14, 30, 0)),  # XML_STRPTIME_YY
        ("2026-05-08T14:30:00", dt.datetime(2026, 5, 8, 14, 30, 0)),  # ISO 8601 fallback
    ],
)
def test_parse_isy_datetime_known_formats(raw: str, expected: dt.datetime) -> None:
    """All four documented ISY datetime formats plus ISO 8601 must parse."""
    assert parse_isy_datetime(raw) == expected


@pytest.mark.parametrize("raw", ["", None, "not a date", "2026-13-99"])
def test_parse_isy_datetime_returns_empty_time_on_failure(raw) -> None:
    """Defensive: anything unparsable → ``EMPTY_TIME`` sentinel,
    never raises. Consumers compare against ``EMPTY_TIME`` to detect
    "no time set" rather than catching exceptions."""
    assert parse_isy_datetime(raw) == EMPTY_TIME


def test_parse_isy_datetime_non_string_input_returns_empty() -> None:
    """The helper accepts ``str | None`` per its signature, but
    runtime garbage (int, dict) shouldn't crash either."""
    assert parse_isy_datetime(12345) == EMPTY_TIME  # type: ignore[arg-type]


# --- convert_isy_raw_value -----------------------------------------------


def test_convert_returns_unknown_sentinel_on_unknown_input() -> None:
    """``ISY_VALUE_UNKNOWN`` is the controller's "no value" sentinel
    (``-inf``). Round-trips through the converter as-is."""
    result = convert_isy_raw_value(ISY_VALUE_UNKNOWN, "100", 0)
    assert math.isinf(result) and result < 0


def test_convert_none_value_is_treated_as_unknown() -> None:
    result = convert_isy_raw_value(None, "100", 0)  # type: ignore[arg-type]
    assert math.isinf(result) and result < 0


@pytest.mark.parametrize(
    ("raw_value", "uom"),
    [
        (136, UOM_DOUBLE_TEMP),  # UOM 101 — Insteon thermostat 0.5° encoding
        (136, UOM_ISYV4_DEGREES),  # legacy alias
    ],
)
def test_convert_halves_values_for_double_temp_uoms(raw_value: int, uom: str) -> None:
    """Insteon thermostats encode 0.5°-precision temps as ``2*temp``.
    UOM 101 (and the legacy ISY-v4 ``"degrees"`` alias) must be
    halved on the way out."""
    assert convert_isy_raw_value(raw_value, uom, 0) == 68.0


def test_convert_applies_decimal_precision() -> None:
    """``value=2345, prec=2`` → ``23.45``. Pin both shape and
    rounding."""
    assert convert_isy_raw_value(2345, "100", 2) == 23.45


def test_convert_precision_can_be_string_form() -> None:
    """The eisy reports precision as a string in event frames; the
    helper must accept either."""
    assert convert_isy_raw_value(2345, "100", "2") == 23.45


def test_convert_zero_precision_passes_value_through() -> None:
    """No precision → return value unchanged (still int)."""
    assert convert_isy_raw_value(42, "100", 0) == 42
    assert convert_isy_raw_value(42, "100", "0") == 42


def test_convert_uses_fallback_precision_when_no_explicit() -> None:
    """Some events ship without a ``prec`` attr but a sensible
    default exists for the property class. The fallback rounds the
    raw value rather than dividing."""
    assert convert_isy_raw_value(3.14159, "100", 0, fallback_precision=2) == 3.14


def test_convert_no_fallback_returns_raw() -> None:
    """No precision and no fallback → leave the value untouched."""
    assert convert_isy_raw_value(3.14159, "100", 0) == 3.14159
