"""Tests for ``parse_rest_status`` against real-controller XML samples.

The fixtures here originated as anonymized PyISY 3.6.1 captures
(``status.xml``, ``status_thermostat.xml``, ``status_zwave_lock.xml``)
and exercise three property-shape variants the synthetic JSON tests
don't cover well:

* dimmer / keypad-load nodes with empty ``value=""`` placeholders
* Insteon thermostat (UOMs 22 / 66 / 98 / 99 / 101)
* Z-Wave lock (UOMs 11 / 51 / 70)

The parser is documented to preserve empty values rather than drop
the property, and to surface the raw ``uom`` string verbatim — the
tests below pin both behaviours."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyisyox.client import (
    ClientError,
    NodeRecord,
    merge_status_into_nodes,
    parse_rest_status,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"


def _load(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


# --- /rest/status: dimmers + keypad loads ---------------------------------


@pytest.fixture(scope="module")
def dimmers_status() -> dict:
    return parse_rest_status(_load("rest_status_dimmers.xml"))


def test_dimmers_status_parses_all_nodes(dimmers_status: dict) -> None:
    """The fixture has 28 distinct ``node[id]`` entries; every one is
    decoded and keyed by its raw address (spaces preserved)."""
    assert len(dimmers_status) >= 25
    assert "5A 90 D6 1" in dimmers_status
    assert all(" " in addr or "_" in addr for addr in dimmers_status)


def test_dimmers_status_preserves_empty_value_placeholders(dimmers_status: dict) -> None:
    """``OL`` and ``RR`` come through with ``value=""`` for sub-nodes
    that don't expose them. Per the parser docstring, the property
    must remain present (don't drop on empty), so consumers can tell
    "controller hasn't reported yet" from "property doesn't exist"."""
    sub = dimmers_status["5A 90 D6 3"]
    assert "OL" in sub
    assert sub["OL"].value == ""
    # The formatted attr is a single space in the source; compare loose.
    assert sub["OL"].formatted.strip() == ""
    assert sub["OL"].uom == "0"


def test_dimmers_status_decodes_dimmer_levels(dimmers_status: dict) -> None:
    """Real ``value``/``formatted``/``uom`` combo, not synthetic."""
    primary = dimmers_status["5A 90 D6 1"]
    assert primary["OL"].value == "153"
    assert primary["OL"].formatted == "60%"
    assert primary["OL"].uom == "100"
    assert primary["RR"].formatted == "0.5 seconds"
    assert primary["ST"].formatted == "Off"


# --- /rest/status: thermostat (UOMs 22, 66, 98, 99, 101) -----------------


def test_thermostat_status_decodes_all_climate_props() -> None:
    """The thermostat fixture exercises five climate-specific UOMs;
    every property must round-trip uom + value + formatted verbatim
    so the editor codec can decode them downstream."""
    status = parse_rest_status(_load("rest_status_thermostat.xml"))
    node = status["91 DD DB 1"]
    assert node["CLIFS"].uom == "99"
    assert node["CLIFS"].formatted == "Auto"
    assert node["CLIHCS"].uom == "66"
    assert node["CLIHCS"].formatted == "Idle"
    assert node["CLIHUM"].uom == "22"
    assert node["CLIMD"].uom == "98"
    assert node["CLIMD"].formatted == "Program Auto"
    # Cool/heat setpoints + current temp share UOM 101 (degrees x 2)
    assert node["CLISPC"].uom == "101"
    assert node["CLISPC"].value == "156"
    assert node["CLISPH"].value == "130"
    assert node["ST"].uom == "101"


# --- /rest/status: Z-Wave lock (UOMs 11, 51, 70) -------------------------


def test_zwave_lock_status_decodes_lock_state() -> None:
    """The lock fixture has two related nodes — primary lock state
    (UOM 11, ``BATLVL`` UOM 51) and an alarm/usrnum subnode."""
    status = parse_rest_status(_load("rest_status_zwave_lock.xml"))
    primary = status["ZY007_1"]
    assert primary["BATLVL"].uom == "51"
    assert primary["BATLVL"].formatted == "60%"
    assert primary["ST"].uom == "11"
    assert primary["ST"].formatted == "Locked"

    sub = status["ZY007_306"]
    assert sub["ALARM"].value == "3"
    assert sub["USRNUM"].uom == "70"


# --- merge_status_into_nodes -----------------------------------------------


def test_merge_status_overlays_onto_existing_nodes() -> None:
    """``merge_status_into_nodes`` must overwrite same-id properties
    with the status payload (status is authoritative) while leaving
    JSON-only properties intact."""
    nodes: dict[str, NodeRecord] = {
        "91 DD DB 1": NodeRecord(
            address="91 DD DB 1",
            name="Hallway Thermostat",
            nodedef_id="2441ZTH",
            family_id="1",
            instance_id="1",
            type="5.16.4.0",
            enabled=True,
            pnode="91 DD DB 1",
            properties={},
        ),
        # Status fixture has no entry for this address → properties stay empty
        "AA AA AA 1": NodeRecord(
            address="AA AA AA 1",
            name="Untouched",
            nodedef_id="x",
            family_id="1",
            instance_id="1",
            enabled=True,
            pnode="AA AA AA 1",
            properties={},
        ),
    }
    status = parse_rest_status(_load("rest_status_thermostat.xml"))
    merge_status_into_nodes(nodes, status)
    assert "CLIMD" in nodes["91 DD DB 1"].properties
    assert nodes["91 DD DB 1"].properties["ST"].formatted == "68.0°"
    assert not nodes["AA AA AA 1"].properties


# --- error paths -----------------------------------------------------------


def test_parse_rest_status_empty_input_returns_empty_dict() -> None:
    assert not parse_rest_status("")


def test_parse_rest_status_skips_node_without_id() -> None:
    """Defensive: a malformed ``<node>`` without ``id=`` is dropped
    rather than raising."""
    xml = """<?xml version="1.0"?><nodes>
        <node id="A 1 2 1"><property id="ST" value="0" formatted="Off" uom="100"/></node>
        <node><property id="ST" value="1" formatted="On" uom="100"/></node>
    </nodes>"""
    out = parse_rest_status(xml)
    assert list(out) == ["A 1 2 1"]


def test_parse_rest_status_skips_property_without_id() -> None:
    """Same shape but the *property* has no id — drop the property,
    keep the node."""
    xml = """<?xml version="1.0"?><nodes>
        <node id="A 1 2 1">
            <property id="ST" value="0" formatted="Off" uom="100"/>
            <property value="x"/>
        </node>
    </nodes>"""
    out = parse_rest_status(xml)
    assert list(out["A 1 2 1"]) == ["ST"]


def test_parse_rest_status_raises_on_malformed_xml() -> None:
    with pytest.raises(ClientError, match="failed to parse"):
        parse_rest_status("<not-closed>")
