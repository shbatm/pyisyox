"""Tests for the NLS string-table parser/lookup (:mod:`pyisyox.schema.nls`)."""

from __future__ import annotations

from pyisyox.schema.nls import GLOBAL_NLS_FAMILY_ID, NLSTable

_SAMPLE = """\
# a comment
   # an indented comment

CMD-FDUP-NAME = Fade Up
CMD-DON-NAME = On
CMD-197-SVOL-NAME = Volume
ST-ST-NAME = Status
ST-197-ST-NAME = Current Tone
NDN-201-NAME = Central Scene Control Button
IX_DIM_REP-0 = Off
IX_DIM_REP-101 = Unknown
IX_DIM_REP-LABEL = ignored
PGM-CMD-CONFIG-FMT = /num//Parameter ${vo}/ /val// = ${v}/
malformed line with no equals
 = empty key
"""


def test_global_family_id_constant() -> None:
    assert GLOBAL_NLS_FAMILY_ID == "-1"


def test_parse_skips_comments_blanks_and_malformed() -> None:
    table = NLSTable.parse(_SAMPLE)
    assert "malformed line with no equals" not in table.entries
    # An empty key (line starting with ``=``) is dropped.
    assert "" not in table.entries
    # Values containing ``=`` survive (split on the first ``=`` only).
    assert table.entries["PGM-CMD-CONFIG-FMT"] == "/num//Parameter ${vo}/ /val// = ${v}/"


def test_command_name_prefers_scoped_then_global() -> None:
    table = NLSTable.parse(_SAMPLE)
    assert table.command_name("FDUP") == "Fade Up"
    assert table.command_name("FDUP", base="197") == "Fade Up"  # falls back to global
    assert table.command_name("SVOL", base="197") == "Volume"  # scoped only
    assert table.command_name("SVOL") is None
    assert table.command_name("NOPE") is None


def test_property_and_nodedef_names() -> None:
    table = NLSTable.parse(_SAMPLE)
    assert table.property_name("ST") == "Status"
    assert table.property_name("ST", base="197") == "Current Tone"
    assert table.property_name("ST", base="999") == "Status"  # scoped miss → global
    assert table.nodedef_name("201") == "Central Scene Control Button"
    assert table.nodedef_name("999") is None


def test_enum_names_only_integer_suffixes() -> None:
    table = NLSTable.parse(_SAMPLE)
    assert table.enum_names("IX_DIM_REP") == {0: "Off", 101: "Unknown"}
    # A near-prefix that isn't followed by ``-`` doesn't bleed in.
    assert table.enum_names("IX_DIM") == {}
    assert table.enum_names("NOPE") == {}


def test_overlay_other_wins() -> None:
    base = NLSTable.parse("CMD-DON-NAME = On\nST-ST-NAME = Status\n")
    over = NLSTable.parse("CMD-DON-NAME = Turn On\nNDN-1-NAME = Thing\n")
    merged = base.overlay(over)
    assert merged.command_name("DON") == "Turn On"
    assert merged.property_name("ST") == "Status"
    assert merged.nodedef_name("1") == "Thing"
    # Originals untouched.
    assert base.command_name("DON") == "On"
