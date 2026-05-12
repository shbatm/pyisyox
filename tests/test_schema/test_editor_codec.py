"""Editor codec tests — bidirectional encode/decode with subset validation."""

from __future__ import annotations

import pytest

from pyisyox.schema import Editor, EditorCodecError, Profile


def test_bool_decode(profile: Profile) -> None:
    bool_editor = profile.find_editor("bool", "10", "10")
    assert bool_editor is not None
    assert bool_editor.decode(0) == "False"
    assert bool_editor.decode(1) == "True"


def test_gallons_decode_with_precision(profile: Profile) -> None:
    gallons = profile.find_editor("GALLONS", "10", "10")
    assert gallons is not None
    # raw 6839 with prec=4 -> 0.6839
    assert gallons.decode(6839) == "0.6839"
    assert gallons.decode(0) == "0.0000"


def test_i_auth_enum_roundtrip(profile: Profile) -> None:
    i_auth = profile.find_editor("I_AUTH", "10", "10")
    assert i_auth is not None
    # Decode: int -> name
    assert i_auth.decode(2) == "Authorized"
    assert i_auth.decode(0) == "Not Started"
    # Encode: name -> int (case-insensitive)
    assert i_auth.encode("Authorized") == 2
    assert i_auth.encode("not started") == 0
    # Encode: int passes through
    assert i_auth.encode(3) == 3


def test_tstat_mode_subset_excludes_fan_only(profile: Profile) -> None:
    """I_TSTAT_MODE lists Fan Only (4) in names but subset='0-3,5-7' excludes it.
    Encode must reject 4 even though decode would accept it for legacy display."""
    mode = profile.find_editor("I_TSTAT_MODE", "1", "1")
    assert mode is not None
    # Valid modes encode fine
    assert mode.encode("Heat") == 1
    assert mode.encode("Program Cool") == 7
    # 4 ("Fan Only") is in names but not in the subset — must reject
    with pytest.raises(EditorCodecError, match="not in subset"):
        mode.encode(4)


def test_unknown_name_raises(profile: Profile) -> None:
    i_auth = profile.find_editor("I_AUTH", "10", "10")
    assert i_auth is not None
    with pytest.raises(EditorCodecError, match="not a recognised name"):
        i_auth.encode("Bogus")


def test_range_validation_without_subset() -> None:
    """For range editors with no subset, encode validates [min, max]."""
    ed = Editor.from_json({"id": "I_TEST", "ranges": [{"uom": "51", "min": 0, "max": 100, "prec": 0}]})
    assert ed.encode(50) == 50
    assert ed.encode(0) == 0
    assert ed.encode(100) == 100
    with pytest.raises(EditorCodecError, match="above max"):
        ed.encode(101)
    with pytest.raises(EditorCodecError, match="below min"):
        ed.encode(-1)


def test_subset_parsing_with_gaps() -> None:
    ed = Editor.from_json(
        {
            "id": "I_GAP",
            "ranges": [
                {
                    "uom": "25",
                    "subset": "0-3,5-7",
                    "names": {
                        "0": "Off",
                        "1": "Heat",
                        "2": "Cool",
                        "3": "Auto",
                        "4": "Fan Only",
                        "5": "ProgAuto",
                        "6": "ProgHeat",
                        "7": "ProgCool",
                    },
                }
            ],
        }
    )
    rng = ed.range_for("25")
    assert rng.subset == {0, 1, 2, 3, 5, 6, 7}
    assert rng.is_valid(3)
    assert not rng.is_valid(4)
    assert rng.is_valid(7)


def test_encode_accepts_float_via_rounding() -> None:
    ed = Editor.from_json({"id": "I_F", "ranges": [{"uom": "17", "min": 0, "max": 120, "prec": 0}]})
    assert ed.encode(72.4) == 72
    assert ed.encode(72.6) == 73


def test_decode_passes_through_when_no_match() -> None:
    """Non-enum integers decode to a precision-formatted numeric string."""
    raw = {"id": "I_NUM", "ranges": [{"uom": "73", "min": 0, "max": 1000, "prec": 0}]}
    ed = Editor.from_json(raw)
    assert ed.decode(42) == "42"


# --- prec-symmetric encode / decode (bug fix 2026-05-09) -----------------


def test_encode_scales_by_prec_for_setpoint_editor() -> None:
    """Setpoint editors store displayed °F/°C with ``prec=1`` — the
    *displayed* value 72.0 °F should encode to raw 720, symmetric with
    decode (which divides raw by 10**prec)."""
    ed = Editor.from_json(
        {"id": "I_CLISPH_F", "ranges": [{"uom": "17", "prec": 1, "min": 0.0, "max": 120.0}]}
    )
    assert ed.encode(72.0) == 720
    assert ed.encode(72.5) == 725
    assert ed.encode(0.0) == 0
    assert ed.encode(120.0) == 1200


def test_encode_decode_round_trip_with_prec() -> None:
    """encode(decode(raw)) ≈ raw for all prec>0 editors that aren't enums."""
    ed = Editor.from_json({"id": "I_CLISPC_C", "ranges": [{"uom": "4", "prec": 1, "min": 5.0, "max": 50.0}]})
    for raw in (50, 100, 220, 500):
        displayed = ed.decode(raw)
        assert ed.encode(float(displayed)) == raw


def test_encode_validates_min_max_against_displayed_value() -> None:
    """min/max in the IoX schema are stored in *displayed* form. The validator
    must compare the user's input against them, not the scaled raw int."""
    ed = Editor.from_json({"id": "I_CLISPC_C", "ranges": [{"uom": "4", "prec": 1, "min": 5.0, "max": 50.0}]})
    # 22.0 °C is in range; raw becomes 220 — must NOT be rejected as ">50"
    assert ed.encode(22.0) == 220
    # 50.0 °C is the inclusive max — accepts, raw 500
    assert ed.encode(50.0) == 500
    # 51.0 °C exceeds max
    with pytest.raises(EditorCodecError, match="above max"):
        ed.encode(51.0)
    # 4.9 °C below min
    with pytest.raises(EditorCodecError, match="below min"):
        ed.encode(4.9)


def test_encode_string_numeric_input_also_scales() -> None:
    """A consumer passing the displayed value as a string (e.g. from a UI
    slider) must encode the same way as the float path."""
    ed = Editor.from_json(
        {"id": "I_CLISPH_F", "ranges": [{"uom": "17", "prec": 1, "min": 0.0, "max": 120.0}]}
    )
    assert ed.encode("72.0") == 720
    assert ed.encode("72.5") == 725


def test_encode_enum_name_skips_prec_scaling() -> None:
    """Enum-name lookups return the raw int verbatim — prec doesn't apply."""
    ed = Editor.from_json(
        {
            "id": "I_TSTAT_MODE",
            "ranges": [
                {
                    "uom": "98",
                    "prec": 0,
                    "subset": "0-3,5-7",
                    "names": {"0": "Off", "1": "Heat", "2": "Cool"},
                }
            ],
        }
    )
    assert ed.encode("Heat") == 1


def test_encode_prec_zero_is_unchanged() -> None:
    """For prec=0 editors (the typical enum/index case) encode passes the
    integer through unchanged — symmetric with the original behaviour."""
    ed = Editor.from_json({"id": "I_PREC0", "ranges": [{"uom": "25", "min": 0, "max": 31, "prec": 0}]})
    assert ed.encode(15) == 15
    assert ed.encode(15.4) == 15  # rounds
    assert ed.encode(15.6) == 16


# --- UOM-101 / "degrees" half-degree encoding ----------------------------
#
# Insteon thermostats (which the user's current setup doesn't include, so
# this isn't covered by the live capture fixtures) encode 0.5°-precision
# temperatures as ``raw = 2 * displayed``. The legacy alias ``"degrees"``
# behaves the same way. We handle it on both encode and decode so a
# profile that does carry such an editor works correctly without each
# consumer having to special-case it.


def test_encode_uom_101_doubles_displayed_value_when_prec_zero() -> None:
    """raw = 2 * displayed for UOM 101 with prec=0."""
    ed = Editor.from_json({"id": "I_TEMP", "ranges": [{"uom": "101", "min": 0, "max": 120, "prec": 0}]})
    assert ed.encode(68) == 136
    assert ed.encode(72.5) == 145
    assert ed.encode(0) == 0


def test_decode_uom_101_halves_raw_value_when_prec_zero() -> None:
    """display = raw / 2 for UOM 101 with prec=0. Symmetric with encode."""
    ed = Editor.from_json({"id": "I_TEMP", "ranges": [{"uom": "101", "min": 0, "max": 120, "prec": 0}]})
    assert ed.decode(136) == "68.0"
    assert ed.decode(145) == "72.5"
    assert ed.decode(0) == "0.0"


def test_uom_degrees_alias_doubles() -> None:
    """The ISY-v4 ``"degrees"`` UOM alias behaves identically."""
    ed = Editor.from_json(
        {"id": "I_TEMP_OLD", "ranges": [{"uom": "degrees", "min": 0, "max": 120, "prec": 0}]}
    )
    assert ed.encode(68) == 136
    assert ed.decode(136) == "68.0"


def test_uom_101_with_prec_one_uses_normal_scaling() -> None:
    """Modern profiles using UOM 101 with prec=1 (rare but possible) get
    normal decimal-prec scaling — *not* additional doubling. The half-
    degree behaviour only kicks in when prec=0 explicitly."""
    ed = Editor.from_json({"id": "I_TEMP_NEW", "ranges": [{"uom": "101", "prec": 1, "min": 0, "max": 120}]})
    assert ed.encode(68.0) == 680  # decimal prec, no doubling
    assert ed.decode(680) == "68.0"


def test_uom_101_validates_min_max_against_displayed_value() -> None:
    """min/max remain in displayed form even for UOM 101 — validation
    runs before the *2 raw conversion."""
    ed = Editor.from_json({"id": "I_TEMP", "ranges": [{"uom": "101", "min": 0, "max": 120, "prec": 0}]})
    assert ed.encode(120) == 240  # inclusive max
    with pytest.raises(EditorCodecError, match="above max"):
        ed.encode(121)


def test_range_parses_step_hint() -> None:
    """An editor range's ``step`` (when present) is parsed as a float; it's
    a UI hint and doesn't affect encode/decode."""
    ed = Editor.from_json(
        {"id": "I_CLISPC_C", "ranges": [{"uom": "4", "prec": 1, "step": 0.5, "min": 5.0, "max": 50.0}]}
    )
    assert ed.ranges[0].step == 0.5
    # Codec behaviour is unchanged by the presence of step.
    assert ed.encode(21.5) == 215
    assert ed.decode(215) == "21.5"


def test_range_step_defaults_to_none() -> None:
    """Ranges without a ``step`` key report ``step is None``."""
    ed = Editor.from_json({"id": "I_BL", "ranges": [{"uom": "51", "min": 0, "max": 100}]})
    assert ed.ranges[0].step is None
