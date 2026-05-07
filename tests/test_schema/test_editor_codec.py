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
    with pytest.raises(EditorCodecError, match="not valid"):
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
    with pytest.raises(EditorCodecError, match="not valid"):
        ed.encode(101)
    with pytest.raises(EditorCodecError, match="not valid"):
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
