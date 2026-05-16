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


def test_encode_sends_value_as_is_controller_scales() -> None:
    """The codec validates but does not rewrite the number — the
    controller scales device-side from the appended UOM. An integral
    value comes back ``int`` (clean URL ``/72``); a fractional one is
    preserved (``/cmd/.../72.4/17``)."""
    ed = Editor.from_json({"id": "I_F", "ranges": [{"uom": "17", "min": 0, "max": 120, "prec": 0}]})
    assert ed.encode(72) == 72
    assert ed.encode(72.0) == 72
    assert isinstance(ed.encode(72.0), int)
    assert ed.encode(72.4) == 72.4
    assert ed.encode(72.6) == 72.6


def test_decode_passes_through_when_no_match() -> None:
    """Non-enum integers decode to a precision-formatted numeric string."""
    raw = {"id": "I_NUM", "ranges": [{"uom": "73", "min": 0, "max": 1000, "prec": 0}]}
    ed = Editor.from_json(raw)
    assert ed.decode(42) == "42"


# --- prec editors: send displayed value, controller scales by UOM --------
#
# The controller scales device-side from the appended UOM (proven:
# /cmd/DON/100/100 -> 39%, /cmd/DON/100/51 -> 100%; eisy UI sends
# /cmd/setTemp/10.4/17). encode no longer rewrites the number; decode
# stays a precision-aware *display* helper (not on the read path), so
# encode and decode are intentionally no longer symmetric.


def test_encode_prec_editor_sends_displayed_value_unscaled() -> None:
    """A ``prec=1`` setpoint editor: the displayed value goes on the
    wire verbatim (``72.5`` → ``72.5``, not ``725``); the controller
    applies the precision from UOM 17."""
    ed = Editor.from_json(
        {"id": "I_CLISPH_F", "ranges": [{"uom": "17", "prec": 1, "min": 0.0, "max": 120.0}]}
    )
    assert ed.encode(72.0) == 72
    assert ed.encode(72.5) == 72.5
    assert ed.encode(0.0) == 0
    assert ed.encode(120.0) == 120


def test_decode_is_independent_display_helper() -> None:
    """``decode`` keeps its precision-aware formatting for display
    helpers and is no longer the inverse of ``encode`` (the controller,
    not the codec, does the raw scaling now)."""
    ed = Editor.from_json({"id": "I_CLISPC_C", "ranges": [{"uom": "4", "prec": 1, "min": 5.0, "max": 50.0}]})
    assert ed.decode(220) == "22.0"
    assert ed.encode(22.0) == 22  # sent as-is, NOT 220


def test_encode_validates_min_max_against_displayed_value() -> None:
    """min/max are stored in *displayed* form; the validator compares the
    user's input against them, and the value is sent unscaled."""
    ed = Editor.from_json({"id": "I_CLISPC_C", "ranges": [{"uom": "4", "prec": 1, "min": 5.0, "max": 50.0}]})
    assert ed.encode(22.0) == 22  # in range, sent as-is
    assert ed.encode(50.0) == 50  # inclusive max
    with pytest.raises(EditorCodecError, match="above max"):
        ed.encode(51.0)
    with pytest.raises(EditorCodecError, match="below min"):
        ed.encode(4.9)


def test_encode_string_numeric_input_sent_as_is() -> None:
    """A consumer passing the displayed value as a string (UI slider)
    encodes the same as the float path — validated, sent unscaled."""
    ed = Editor.from_json(
        {"id": "I_CLISPH_F", "ranges": [{"uom": "17", "prec": 1, "min": 0.0, "max": 120.0}]}
    )
    assert ed.encode("72.0") == 72
    assert ed.encode("72.5") == 72.5


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


def test_encode_integer_passes_through() -> None:
    """An integral value is sent unchanged as ``int`` (clean URL); a
    fractional value is preserved (the controller scales by UOM)."""
    ed = Editor.from_json({"id": "I_PREC0", "ranges": [{"uom": "25", "min": 0, "max": 31, "prec": 0}]})
    assert ed.encode(15) == 15
    assert isinstance(ed.encode(15.0), int)
    assert ed.encode(15.4) == 15.4
    assert ed.encode(15.6) == 15.6


# --- UOM-101 / "degrees" half-degree ------------------------------------
#
# Insteon thermostats encode 0.5°-precision temps as ``raw = 2 *
# displayed``. The controller does that scaling itself from UOM 101 (no
# Insteon-thermostat hardware in the live capture; the rule is the same
# proven one — ``/cmd`` sends the displayed value + UOM). ``decode``
# still halves for display helpers (independent of encode now).


def test_encode_uom_101_sends_displayed_value_no_client_doubling() -> None:
    """UOM 101: the displayed value goes on the wire as-is; the
    controller applies the half-degree raw doubling, not the codec."""
    ed = Editor.from_json({"id": "I_TEMP", "ranges": [{"uom": "101", "min": 0, "max": 120, "prec": 0}]})
    assert ed.encode(68) == 68
    assert ed.encode(72.5) == 72.5
    assert ed.encode(0) == 0


def test_decode_uom_101_halves_raw_value_when_prec_zero() -> None:
    """``decode`` still halves UOM-101 raw for display helpers (it is no
    longer the inverse of ``encode``)."""
    ed = Editor.from_json({"id": "I_TEMP", "ranges": [{"uom": "101", "min": 0, "max": 120, "prec": 0}]})
    assert ed.decode(136) == "68.0"
    assert ed.decode(145) == "72.5"
    assert ed.decode(0) == "0.0"


def test_uom_degrees_alias_sends_displayed_value() -> None:
    """The ISY-v4 ``"degrees"`` alias also sends the displayed value
    unscaled; ``decode`` still halves for display."""
    ed = Editor.from_json(
        {"id": "I_TEMP_OLD", "ranges": [{"uom": "degrees", "min": 0, "max": 120, "prec": 0}]}
    )
    assert ed.encode(68) == 68
    assert ed.decode(136) == "68.0"


def test_uom_101_prec_one_also_sends_displayed_value() -> None:
    """UOM 101 with prec=1 — still sent unscaled (controller scales);
    ``decode`` formats by prec for display."""
    ed = Editor.from_json({"id": "I_TEMP_NEW", "ranges": [{"uom": "101", "prec": 1, "min": 0, "max": 120}]})
    assert ed.encode(68.0) == 68
    assert ed.decode(680) == "68.0"


def test_uom_101_validates_min_max_against_displayed_value() -> None:
    """min/max stay in displayed form for UOM 101; the value is sent
    unscaled after validation."""
    ed = Editor.from_json({"id": "I_TEMP", "ranges": [{"uom": "101", "min": 0, "max": 120, "prec": 0}]})
    assert ed.encode(120) == 120  # inclusive max, unscaled
    with pytest.raises(EditorCodecError, match="above max"):
        ed.encode(121)


def test_range_parses_step_hint() -> None:
    """An editor range's ``step`` (when present) is parsed as a float; it's
    a UI hint and doesn't affect encode/decode."""
    ed = Editor.from_json(
        {"id": "I_CLISPC_C", "ranges": [{"uom": "4", "prec": 1, "step": 0.5, "min": 5.0, "max": 50.0}]}
    )
    assert ed.ranges[0].step == 0.5
    # Codec behaviour is unaffected by step: value sent as-is.
    assert ed.encode(21.5) == 21.5
    assert ed.decode(215) == "21.5"


def test_range_step_defaults_to_none() -> None:
    """Ranges without a ``step`` key report ``step is None``."""
    ed = Editor.from_json({"id": "I_BL", "ranges": [{"uom": "51", "min": 0, "max": 100}]})
    assert ed.ranges[0].step is None


# --- encoded editor ids ---------------------------------------------------


def test_from_encoded_id_uom_prec_only() -> None:
    """``_<uom>_<prec>`` — implied unbounded int range, given precision."""
    ed = Editor.from_encoded_id("_1_3")
    assert ed is not None
    assert ed.id == "_1_3"
    (rng,) = ed.ranges
    assert (rng.uom, rng.precision, rng.min, rng.max, rng.subset) == ("1", 3, None, None, set())


def test_from_encoded_id_numeric_range_and_negatives() -> None:
    """``_R_<min>_<max>`` with the ``m`` = negative convention."""
    assert Editor.from_encoded_id("_56_0_R_0_255").ranges[0].max == 255  # type: ignore[union-attr]
    ed = Editor.from_encoded_id("_17_2_R_m5_10")
    assert ed is not None
    rng = ed.ranges[0]
    assert (rng.uom, rng.precision, rng.min, rng.max) == ("17", 2, -5, 10)


def test_from_encoded_id_subset_bitmasks() -> None:
    """``_S_<lowMask>[_<highMask>]`` decodes the hex bitmask to a value set."""
    assert Editor.from_encoded_id("_17_1_S_FF00FF00").ranges[0].subset == set(range(8, 16)) | set(  # type: ignore[union-attr]
        range(24, 32)
    )
    assert Editor.from_encoded_id("_17_1_S_FF00FF00_03E").ranges[0].subset == (  # type: ignore[union-attr]
        set(range(8, 16)) | set(range(24, 32)) | set(range(33, 38))
    )


def test_from_encoded_id_captures_trailing_nls_prefix() -> None:
    """A ``_N_<nls>`` tail (which can itself contain ``_``) is captured as
    ``EditorRange.nls_prefix`` — ``names`` stays empty until something
    resolves it against an NLS table."""
    ed = Editor.from_encoded_id("_51_0_R_0_101_N_IX_DIM_REP")
    assert ed is not None
    rng = ed.ranges[0]
    assert (rng.uom, rng.precision, rng.min, rng.max) == ("51", 0, 0, 101)
    assert rng.nls_prefix == "IX_DIM_REP"
    assert rng.names == {}
    # No ``_N_`` segment ⇒ no prefix.
    assert Editor.from_encoded_id("_1_3").ranges[0].nls_prefix is None  # type: ignore[union-attr]


@pytest.mark.parametrize("bad", ["ZW_DIM_PERCENT", "_sys_notify_full", "_17_x", "_17", "_", "", "_17_1_Q_5"])
def test_from_encoded_id_rejects_non_encodings(bad: str) -> None:
    assert Editor.from_encoded_id(bad) is None


def test_encode_param_picks_accepting_range_and_returns_its_uom() -> None:
    """A multi-range editor (range 0: tiny ``{1: "Previous Value"}`` index
    in uom 25; range 1: 0-100 % in uom 51) — a plain percent value must be
    encoded by the % range and report uom ``"51"``, while the index value
    ``1`` still resolves (and reports uom ``"25"``)."""
    ed = Editor.from_json(
        {
            "id": "ZW_DIM_PERCENT_LIKE",
            "ranges": [
                {"uom": "25", "subset": "1", "names": {"1": "Previous Value"}},
                {"uom": "51", "min": 0, "max": 100},
            ],
        }
    )
    assert ed.encode_param(75) == (75, "51")
    assert ed.encode_param("Previous Value") == (1, "25")
    assert ed.encode_param(1) == (1, "25")  # first range wins for the ambiguous int
    assert ed.encode(75) == 75  # plain encode() still works
    # decode scans every range for an enum name when no uom hint is given
    assert ed.decode(1) == "Previous Value"
    assert ed.decode(75) == "75"
    with pytest.raises(EditorCodecError):
        ed.encode_param(150)  # out of every range
