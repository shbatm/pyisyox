"""Tests for the JSON + XML parsers in :mod:`pyisyox.client`."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from pyisyox.client import (
    ClientError,
    NodePropertyValue,
    VariableRecord,
    _unwrap_data,
    merge_status_into_nodes,
    parse_api_nodes,
    parse_api_programs,
    parse_api_variables_type,
    parse_rest_networking_resources,
    parse_rest_status,
    parse_zwave_nodedefs,
)

FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"

# --- /api/nodes JSON ------------------------------------------------------


def test_parse_api_nodes_native_with_inline_property() -> None:
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {
                        "address": "3D 7D 87 1",
                        "name": "Breezeway",
                        "nodeDefId": "KeypadDimmer_ADV",
                        "type": "1.65.69.0",
                        "enabled": "true",
                        "pnode": "3D 7D 87 1",
                        "property": [
                            {"id": "ST", "value": "0", "formatted": "Off", "uom": "100", "name": ""}
                        ],
                    }
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    node = nodes["3D 7D 87 1"]
    assert node.nodedef_id == "KeypadDimmer_ADV"
    assert node.family_id == "1"  # native => default family
    assert node.instance_id == "1"
    assert node.properties["ST"].formatted == "Off"


def test_parse_api_nodes_plugin_node_carries_no_properties() -> None:
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {
                        "address": "n010_84dd4c2c24c3b7",
                        "name": "Flume Sensor",
                        "nodeDefId": "flume2",
                        "family": {"_": "10", "instance": "10"},
                        "type": "1.2.3.4",
                        "enabled": "true",
                        "parent": {"_": "n010_controller", "type": "1"},
                        "pnode": "n010_controller",
                    }
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    node = nodes["n010_84dd4c2c24c3b7"]
    assert node.family_id == "10"
    assert node.instance_id == "10"
    assert node.parent_address == "n010_controller"
    assert node.properties == {}, "plugin nodes must arrive empty in /api/nodes"


def test_parse_api_nodes_bare_scalar_family() -> None:
    """Built-in non-Insteon families (Z-Wave, Z-Matter-Z-Wave, …) give
    ``family`` as a plain string on /api/nodes — profile instance is "1"."""
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {
                        "address": "ZW002_1",
                        "name": "ZW 002 Dimmer Switch",
                        "nodeDefId": "UZW000E",
                        "family": "4",  # bare string, not {"_": ..., "instance": ...}
                        "type": "4.17.1.0",
                        "enabled": "true",
                        "pnode": "ZW002_1",
                        "property": [{"id": "ST", "value": "37", "formatted": "37%", "uom": "51"}],
                    },
                    {
                        "address": "n012_1",
                        "name": "ZMatter node",
                        "nodeDefId": "UZM0001",
                        "family": 12,  # int form, also handled
                        "type": "12.1.1.0",
                        "enabled": "true",
                    },
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    zw = nodes["ZW002_1"]
    assert (zw.family_id, zw.instance_id) == ("4", "1")
    assert zw.properties["ST"].value == "37"
    zm = nodes["n012_1"]
    assert (zm.family_id, zm.instance_id) == ("12", "1")


def test_parse_api_nodes_handles_empty_payload() -> None:
    assert parse_api_nodes({}) == {}
    assert parse_api_nodes({"data": {}}) == {}
    assert parse_api_nodes({"data": {"nodes": {}}}) == {}


def test_parse_api_nodes_zwave_devtype_block_lands_on_record() -> None:
    """Z-Wave / Z-Matter nodes carry a ``devtype`` JSON object —
    ``cat`` (generic class), ``mfg`` (mfr.prod_type.product), ``gen``
    (basic.generic.specific). Adapted from the legacy PyISY
    ``ZWaveProperties`` so consumers can sort by Z-Wave generic
    category without re-parsing the JSON.
    """
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {
                        "address": "ZW019_1",
                        "name": "ZW 019 Multi-Channel Sensor",
                        "nodeDefId": "UZW0099",
                        "family": "4",
                        "type": "4.16.1.0",
                        "enabled": "true",
                        "devtype": {
                            "gen": "4.16.1",
                            "mfg": "634.257.13",
                            "cat": "121",
                        },
                    },
                    {
                        # Insteon node — no devtype block.
                        "address": "1A 2B 3C 1",
                        "name": "Hallway Switch",
                        "nodeDefId": "DimmerLampSwitch",
                        "type": "1.65.69.0",
                    },
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)

    zw = nodes["ZW019_1"].zwave_props
    assert zw is not None
    assert zw.category == "121"
    assert zw.devtype_mfg == "634.257.13"
    assert zw.devtype_gen == "4.16.1"
    assert (zw.basic_type, zw.generic_type, zw.specific_type) == ("4", "16", "1")
    assert (zw.mfr_id, zw.prod_type_id, zw.product_id) == ("634", "257", "13")

    # Insteon nodes get None — the gate consumers use to skip the lookup.
    assert nodes["1A 2B 3C 1"].zwave_props is None


def test_parse_api_nodes_skips_property_entries_without_id() -> None:
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {
                        "address": "X",
                        "nodeDefId": "X",
                        "property": [{"value": "5"}, {"id": "ST", "value": "1"}],
                    }
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    assert set(nodes["X"].properties) == {"ST"}


# --- /rest/status XML -----------------------------------------------------


def test_parse_rest_status_native_and_plugin_uniformly() -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<nodes>
  <node id="3D 7D 87 1">
    <property id="OL" value="153" formatted="60%" uom="100" name="" />
    <property id="ST" value="0" formatted="Off" uom="100" name="" />
  </node>
  <node id="n010_84dd4c2c24c3b7">
    <property id="ST" value="1" formatted="True" uom="2" />
    <property id="GV1" value="6839" formatted="0.6839 US gallons" uom="69" />
  </node>
</nodes>"""
    out = parse_rest_status(xml)
    assert set(out) == {"3D 7D 87 1", "n010_84dd4c2c24c3b7"}
    flume = out["n010_84dd4c2c24c3b7"]
    assert flume["GV1"].formatted == "0.6839 US gallons"
    assert flume["ST"].uom == "2"


def test_parse_rest_status_empty_string_returns_empty_dict() -> None:
    assert parse_rest_status("") == {}


def test_parse_rest_status_preserves_empty_value_attrs() -> None:
    """Insteon thermostats and unconfigured nodes report value="" — keep
    those entries (drop them and the consumer can't distinguish 'no value
    yet' from 'property does not exist')."""
    xml = '<nodes><node id="X"><property id="OL" value="" formatted=" " uom="0"/></node></nodes>'
    out = parse_rest_status(xml)
    assert out["X"]["OL"].value == ""
    assert out["X"]["OL"].uom == "0"


# --- prec field on NodePropertyValue --------------------------------------
#
# ``prec`` is the controller-declared decimal scaling for a property's raw
# ``value``: the displayed reading is ``raw / 10**prec``. It arrives on every
# wire shape; consumers (e.g. an HA sensor entity) read it to format numeric
# state.


def test_parse_api_nodes_captures_prec_from_json() -> None:
    """``/api/nodes`` JSON puts ``prec`` next to ``value`` on each property."""
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {
                        "address": "X",
                        "property": [
                            {"id": "GV1", "value": "6839", "uom": "69", "prec": 4},
                            {"id": "ST", "value": "1", "uom": "2", "prec": 0},
                        ],
                    }
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    assert nodes["X"].properties["GV1"].precision == 4
    assert nodes["X"].properties["ST"].precision == 0


def test_parse_api_nodes_omitted_prec_defaults_to_zero() -> None:
    """Properties without scaling (Insteon ``ST``, plugin enums) typically
    omit ``prec`` entirely — falling through to ``0`` keeps ``raw / 10**0``
    a no-op."""
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {
                        "address": "X",
                        "property": [{"id": "ST", "value": "0", "uom": "100"}],
                    }
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    assert nodes["X"].properties["ST"].precision == 0


def test_parse_rest_status_captures_prec_from_xml_attr() -> None:
    """``/rest/status`` puts ``prec`` on the ``<property>`` XML attr."""
    xml = (
        '<nodes><node id="X">'
        '<property id="GV1" value="6839" formatted="0.6839" uom="69" prec="4"/>'
        '<property id="ST" value="1" formatted="On" uom="100"/>'
        "</node></nodes>"
    )
    out = parse_rest_status(xml)
    assert out["X"]["GV1"].precision == 4
    # ST entry has no prec attribute — default applies.
    assert out["X"]["ST"].precision == 0


def test_parse_status_handles_non_numeric_prec_defensively() -> None:
    """A blank or junk ``prec`` shouldn't poison the parse — coerce to 0."""
    xml = '<nodes><node id="X"><property id="GV1" value="" formatted="" uom="" prec=""/></node></nodes>'
    assert parse_rest_status(xml)["X"]["GV1"].precision == 0


def test_node_property_value_default_prec_is_zero() -> None:
    """Construction without ``prec`` defaults to 0 — preserves backwards-
    compatible NodePropertyValue construction at the API level."""
    npv = NodePropertyValue(id="ST", value="0")
    assert npv.precision == 0


# --- merge ---------------------------------------------------------------


def test_merge_overlays_status_over_json() -> None:
    nodes = parse_api_nodes(
        {
            "data": {
                "nodes": {
                    "node": [
                        {
                            "address": "A",
                            "nodeDefId": "X",
                            "property": [{"id": "ST", "value": "0", "formatted": "Off", "uom": "100"}],
                        }
                    ]
                }
            }
        }
    )
    status = {"A": {"ST": NodePropertyValue(id="ST", value="255", formatted="On", uom="100")}}
    merge_status_into_nodes(nodes, status)
    assert nodes["A"].properties["ST"].formatted == "On", "status is authoritative"


def test_merge_fills_in_plugin_node_properties() -> None:
    nodes = parse_api_nodes(
        {
            "data": {
                "nodes": {
                    "node": [
                        {
                            "address": "n010_x",
                            "nodeDefId": "flume2",
                            "family": {"_": "10", "instance": "10"},
                        }
                    ]
                }
            }
        }
    )
    assert nodes["n010_x"].properties == {}, "plugin node arrives empty"

    status = {
        "n010_x": {
            "ST": NodePropertyValue(id="ST", value="1", formatted="True", uom="2"),
            "GV1": NodePropertyValue(id="GV1", value="6839", formatted="0.6839 gal", uom="69"),
        }
    }
    merge_status_into_nodes(nodes, status)
    assert set(nodes["n010_x"].properties) == {"ST", "GV1"}


def test_merge_preserves_json_only_properties() -> None:
    """Unusual but possible: a property reported in /api/nodes but not in
    /rest/status. Keep it — dropping would mask data the controller did
    surface."""
    nodes = parse_api_nodes(
        {
            "data": {
                "nodes": {
                    "node": [
                        {
                            "address": "A",
                            "nodeDefId": "X",
                            "property": [{"id": "RR", "value": "28"}],
                        }
                    ]
                }
            }
        }
    )
    status = {"A": {"ST": NodePropertyValue(id="ST", value="0", uom="100")}}
    merge_status_into_nodes(nodes, status)
    assert set(nodes["A"].properties) == {"RR", "ST"}


def test_merge_no_status_for_address_is_noop() -> None:
    nodes = parse_api_nodes({"data": {"nodes": {"node": [{"address": "A", "nodeDefId": "X"}]}}})
    merge_status_into_nodes(nodes, {})
    assert nodes["A"].properties == {}


# --- _unwrap_data envelope handling --------------------------------------


def test_unwrap_data_returns_list_on_success() -> None:
    items = [{"id": "1", "name": "X"}]
    assert _unwrap_data({"successful": True, "data": items}) == items


def test_unwrap_data_returns_empty_when_data_missing_or_wrong_type() -> None:
    assert _unwrap_data({"successful": True}) == []
    assert _unwrap_data({"successful": True, "data": {"k": "v"}}) == []


def test_unwrap_data_raises_on_successful_false_with_error() -> None:
    """Server-side error envelopes used to silently flatten to []. The fix
    raises ClientError so consumers see the actual failure instead of
    'oh, no programs configured'."""
    with pytest.raises(ClientError, match="successful=false"):
        _unwrap_data({"successful": False, "error": "internal"})


def test_unwrap_data_includes_source_label_in_error() -> None:
    with pytest.raises(ClientError, match="/api/programs"):
        _unwrap_data({"successful": False}, source="/api/programs")


def test_unwrap_data_passes_through_non_dict() -> None:
    """A non-dict response (e.g. a bare list from a legacy endpoint) is
    treated as not-an-envelope and yields []."""
    assert _unwrap_data([{"id": "1"}]) == []
    assert _unwrap_data(None) == []


# --- /api/nodes JSON flag field ------------------------------------------


def test_parse_api_nodes_reads_flag_as_string() -> None:
    """The controller stringifies ``flag`` (e.g. ``"128"`` for
    DEVICE_ROOT) — coerce to int."""
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {"address": "X", "nodeDefId": "Y", "flag": "128"},
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    assert nodes["X"].flag == 128


def test_parse_api_nodes_reads_flag_as_int() -> None:
    """A future firmware shipping the field as a JSON number is also
    accepted without coercion drama."""
    raw = {"data": {"nodes": {"node": [{"address": "X", "nodeDefId": "Y", "flag": 64}]}}}
    assert parse_api_nodes(raw)["X"].flag == 64


def test_parse_api_nodes_flag_defaults_to_zero_when_absent_or_unparseable() -> None:
    raw = {
        "data": {
            "nodes": {
                "node": [
                    {"address": "X", "nodeDefId": "Y"},
                    {"address": "Z", "nodeDefId": "Y", "flag": "not-an-int"},
                ]
            }
        }
    }
    nodes = parse_api_nodes(raw)
    assert nodes["X"].flag == 0
    assert nodes["Z"].flag == 0


# --- /rest/networking/resources XML --------------------------------------


def test_parse_rest_networking_resources_extracts_records() -> None:
    """Two resources, surfaced with their id (as string) + name. The
    runtime wrapper :class:`pyisyox.runtime.NetworkResource` then
    fires by id."""
    xml = (
        '<?xml version="1.0"?>'
        "<NetConfig>"
        "<NetRule><id>1</id><name>Reboot Router</name><host>192.0.2.1</host></NetRule>"
        "<NetRule><id>2</id><name>Notify</name></NetRule>"
        "</NetConfig>"
    )
    records = parse_rest_networking_resources(xml)
    assert list(records) == ["1", "2"]
    assert records["1"].name == "Reboot Router"
    assert records["2"].address == "2"


def test_parse_rest_networking_resources_handles_empty_or_missing() -> None:
    """Controllers without the networking module return an empty
    ``<NetConfig/>``. Empty input also flatten to ``{}`` so optional-
    module endpoints don't abort initial load."""
    assert parse_rest_networking_resources("") == {}
    assert parse_rest_networking_resources('<?xml version="1.0"?><NetConfig/>') == {}


def test_parse_rest_networking_resources_skips_rules_without_id() -> None:
    """Defensive — a malformed ``<NetRule>`` without ``<id>`` is
    dropped rather than added with an empty key."""
    xml = (
        '<?xml version="1.0"?>'
        "<NetConfig>"
        "<NetRule><name>NoId</name></NetRule>"
        "<NetRule><id>5</id><name>Real</name></NetRule>"
        "</NetConfig>"
    )
    records = parse_rest_networking_resources(xml)
    assert list(records) == ["5"]


def test_parse_rest_networking_resources_repairs_unescaped_ampersand(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """eisy firmware emits raw ``&`` in network-resource URLs, making
    ``/rest/networking`` not well-formed. The parser repairs bare
    ampersands and recovers the resources rather than aborting the
    controller load (issue #156)."""
    xml = (
        '<?xml version="1.0"?>'
        "<NetConfig>"
        "<NetRule><id>1</id><name>Webhook</name>"
        "<url>http://host/api?a=1&b=2&c=3</url></NetRule>"
        "<NetRule><id>2</id><name>Plain &amp; Fine</name></NetRule>"
        "</NetConfig>"
    )
    with caplog.at_level(logging.WARNING):
        records = parse_rest_networking_resources(xml)
    assert list(records) == ["1", "2"]
    # Pre-existing valid entities are untouched by the repair.
    assert records["2"].name == "Plain & Fine"
    assert "Repaired malformed /rest/networking XML" in caplog.text


def test_parse_rest_networking_resources_unrepairable_degrades_to_empty(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A malformation the ampersand repair can't fix (here, an
    unclosed tag) must not abort the controller load — it degrades to
    ``{}`` with an error log (issue #156)."""
    with caplog.at_level(logging.ERROR):
        assert parse_rest_networking_resources("<not really xml") == {}
    assert "network resources will be unavailable" in caplog.text


# --- /api/programs JSON --------------------------------------------------


def test_parse_api_programs_distinguishes_folders_and_programs() -> None:
    """Folders carry no enabled / running fields; programs do."""
    raw = [
        {
            "id": "0001",
            "name": "Programs Root",
            "folder": True,
            "status": "true",
            "lastFinishTime": "",
            "lastRunTime": "",
            "nextScheduledRunTime": "",
        },
        {
            "id": "0011",
            "name": "Sample Program",
            "parentId": "0001",
            "folder": False,
            "status": "false",
            "enabled": True,
            "runAtStartup": False,
            "running": "idle",
            "lastFinishTime": "2026-05-10T14:46:36.000Z",
            "lastRunTime": "2026-05-10T14:46:36.000Z",
            "nextScheduledRunTime": "",
        },
    ]
    records = parse_api_programs(raw)
    assert set(records) == {"0001", "0011"}

    folder = records["0001"]
    assert folder.is_folder is True
    assert folder.status is True  # "true" string → bool
    assert folder.enabled is None
    assert folder.running is None
    assert folder.last_run_time is None  # empty string → None

    program = records["0011"]
    assert program.is_folder is False
    assert program.status is False
    assert program.enabled is True
    assert program.run_at_startup is False
    assert program.running == "idle"
    assert program.last_run_time == "2026-05-10T14:46:36.000Z"
    assert program.next_scheduled_run_time is None


def test_parse_api_programs_reconstructs_path_from_parent_chain() -> None:
    """A nested HA-style program shows its full slash-joined ancestry,
    minus the synthetic root container ("My Programs"). Drives the
    consumer's HA.<platform>/<name>/<status|actions> classifier."""
    raw = [
        {"id": "0001", "name": "My Programs", "folder": True, "status": "true"},
        {
            "id": "0010",
            "name": "HA.switch",
            "folder": True,
            "status": "true",
            "parentId": "0001",
        },
        {
            "id": "0020",
            "name": "Foo",
            "folder": True,
            "status": "true",
            "parentId": "0010",
        },
        {
            "id": "0030",
            "name": "status",
            "folder": False,
            "status": "true",
            "enabled": True,
            "parentId": "0020",
        },
    ]
    records = parse_api_programs(raw)
    assert records["0030"].path == "HA.switch/Foo/status"
    # Parent folders also expose path (relative to root drop).
    assert records["0020"].path == "HA.switch/Foo"
    # The synthetic root has no parent and gets an empty path.
    assert records["0001"].path == ""


def test_parse_api_programs_handles_real_capture() -> None:
    """End-to-end against the captured (anonymized) eisy fixture."""
    raw = json.loads((FIXTURE_DIR / "api_programs.json").read_text())["data"]

    records = parse_api_programs(raw)
    assert len(records) == len(raw)
    folders = [r for r in records.values() if r.is_folder]
    programs = [r for r in records.values() if not r.is_folder]
    assert folders, "fixture should have at least one folder"
    assert programs, "fixture should have at least one program"
    # Every program reports an enabled flag (real captures always
    # carry it; defensive parser keeps the type Optional).
    assert all(p.enabled is not None for p in programs)


def test_parse_api_programs_handles_empty_payload() -> None:
    assert parse_api_programs([]) == {}


def test_parse_api_programs_skips_entries_without_id() -> None:
    """Defensive — a malformed wire entry without ``id`` is dropped
    rather than added with an empty key."""
    raw = [
        {"id": "", "name": "no-id", "folder": True, "status": "true"},
        {"id": "0011", "name": "good", "folder": False, "status": "false"},
    ]
    records = parse_api_programs(raw)
    assert list(records) == ["0011"]


def test_parse_api_programs_upconverts_decimal_json_int_id_to_hex() -> None:
    """Current IoX firmware reports ``id`` / ``parentId`` as a plain
    decimal JSON *number* (``"id": 149``, per issue #193) rather than
    the classic hex id older firmware sent as a JSON string. The
    legacy ``/rest/programs/{id}/...`` command endpoint 404s on
    decimal, so the parser upconverts to hex -- keyed on the wire
    type (``int``), not the string shape, so it can tell a real
    decimal id apart from an already-hex id that merely looks
    numeric (see the next test)."""
    raw = [
        {"id": 6, "name": "HA.switch", "folder": True, "status": "true"},
        {"id": 149, "name": "actions", "parentId": 6, "folder": False, "status": "true", "enabled": True},
    ]
    records = parse_api_programs(raw)
    assert set(records) == {"0006", "0095"}
    assert records["0095"].address == "0095"
    assert records["0095"].parent_address == "0006"


def test_parse_api_programs_decimal_root_parent_id_zero_collapses_to_none() -> None:
    """A root-level entry's ``parentId`` may arrive as the JSON
    integer ``0`` rather than an omitted key. That must still collapse
    to ``parent_address=None`` (same "no parent" convention ``_path()``
    already applies) -- not upconvert to the dangling ``"0000"`` (id
    ``0`` entries are excluded from the registry, and ``"0000"`` would
    violate ``parent_address``'s "``None`` for the root" contract)."""
    raw = [{"id": 6, "parentId": 0, "name": "HA.switch", "folder": True, "status": "true"}]
    records = parse_api_programs(raw)
    assert records["0006"].parent_address is None


def test_parse_api_programs_preserves_numeric_looking_hex_string_id() -> None:
    """An already-hex id from older firmware that happens to look
    like a decimal number (``"0010"`` meaning hex 0x10, i.e. decimal
    16) must NOT be reparsed as decimal -- that would silently
    corrupt it (to ``"000A"``). Since it arrives as a JSON *string*
    (not a number), it passes through untouched."""
    raw = [{"id": "0010", "name": "Legacy Folder", "folder": True, "status": "true"}]
    records = parse_api_programs(raw)
    assert records["0010"].address == "0010"


# --- /api/variables/{type} ------------------------------------------------


def test_parse_api_variables_type_extracts_typed_records() -> None:
    """Wire fields (``id`` / ``val`` / ``init`` / ``prec`` / ``name`` / ``ts``)
    round-trip onto :class:`VariableRecord`, with the wire ``val`` renamed
    to ``value`` for ergonomic reads."""
    raw = json.loads((FIXTURE_DIR / "api_variables_state.json").read_text())
    records = parse_api_variables_type(_unwrap_data(raw, source="test"), "2")

    assert set(records) == {"1", "2", "5", "8", "17"}
    state_1 = records["1"]
    assert state_1.type_id == "2"
    assert state_1.id == "1"
    assert state_1.name == "State_1"
    assert state_1.value == 0
    assert state_1.init == 0
    assert state_1.precision == 0
    assert state_1.ts == "2026-05-07T21:16:44.000Z"

    # Composite address joins type+id for downstream unique-id derivation.
    assert state_1.address == "2.1"

    # ``prec`` carries through for variables that need decimal scaling.
    calib = records["17"]
    assert calib.precision == 2
    assert calib.value == 12345


def test_parse_api_variables_type_stamps_type_id_on_every_record() -> None:
    """The wire payload doesn't carry the variable type — :func:`parse_api_variables_type`
    receives it as an argument and stamps it onto each record so downstream
    consumers can route mutations back to ``/api/variables/{type}/{id}`` without
    threading the type separately."""
    raw = json.loads((FIXTURE_DIR / "api_variables_int.json").read_text())
    records = parse_api_variables_type(_unwrap_data(raw, source="test"), "1")
    assert all(record.type_id == "1" for record in records.values())


def test_parse_api_variables_type_handles_empty_payload() -> None:
    """Controllers without state variables return an empty data list — the
    parser should produce an empty dict, not raise."""
    assert parse_api_variables_type([], "2") == {}


def test_parse_api_variables_type_skips_entries_without_id() -> None:
    """Defensive: an entry without an ``id`` field is dropped rather than
    added with an empty key."""
    raw = [
        {"id": "", "name": "no-id", "val": 0},
        {"name": "missing-id-key", "val": 0},
        {"id": "5", "name": "good", "val": 1, "init": 0, "prec": 0},
    ]
    records = parse_api_variables_type(raw, "1")
    assert list(records) == ["5"]


def test_parse_api_variables_type_coerces_non_int_values_to_zero() -> None:
    """Junk values in ``val`` / ``init`` / ``prec`` collapse to zero rather
    than raising — preserves the parse-permissively contract that
    ``parse_rest_status`` follows for empty XML attrs."""
    raw = [{"id": "1", "name": "garbage", "val": "abc", "init": "", "prec": None}]
    record = parse_api_variables_type(raw, "1")["1"]
    assert (record.value, record.init, record.precision) == (0, 0, 0)


def test_variable_record_default_construction() -> None:
    """Construction with just type_id + id + name produces sensible defaults
    (zero value/init/prec, empty timestamp)."""
    record = VariableRecord(type_id="1", id="42", name="Spare")
    assert record.value == 0
    assert record.init == 0
    assert record.precision == 0
    assert record.ts == ""
    assert record.address == "1.42"


# --- /rest/zwave/node/{addr}/def/get XML ---------------------------------


def test_parse_zwave_nodedefs_from_fixture() -> None:
    """The dynamically-generated Z-Wave nodedefs arrive as legacy
    ``<nodeDefs><nodedef><sts><st>../<cmds>../<links>..>`` XML; parse one
    full nodedef end-to-end and stamp the supplied family/instance on it."""
    xml = (FIXTURE_DIR / "rest_zwave_nodedefs.xml").read_text()
    nds = parse_zwave_nodedefs(xml, family_id="4", instance_id="1")
    assert set(nds) == {f"UZW{n:04X}" for n in range(0x0E, 0x17)}

    dimmer = nds["UZW000E"]
    assert dimmer.lookup_key == ("UZW000E", "4", "1")
    assert dimmer.nls_key == "109"
    # <sts><st id="ST" editor="_51_0_R_0_101_N_IX_DIM_REP"/><st id="ERR" ... hide="T"/>
    assert dimmer.properties["ST"].editor_id == "_51_0_R_0_101_N_IX_DIM_REP"
    assert dimmer.properties["ERR"].hide is True
    # DON carries two optional params; BRT/DIM are non-native, zero-arg.
    don = next(c for c in dimmer.cmds.accepts if c.id == "DON")
    assert [(p.param_id, p.editor_id, p.optional) for p in don.parameters] == [
        ("", "ZW_DIM_PERCENT", True),
        ("RR", "ZW_RR", True),
    ]
    brt = next(c for c in dimmer.cmds.accepts if c.id == "BRT")
    assert brt.parameters == [] and brt.native is False
    assert next(c for c in dimmer.cmds.accepts if c.id == "DOF").native is True
    # links: this nodedef has only response links
    assert dimmer.links.ctl == []
    assert "MLSW" in dimmer.links.rsp
    # a nodedef with an empty <sts/> still parses
    assert nds["UZW0010"].properties == {}


def test_parse_zwave_nodedefs_empty_input() -> None:
    assert parse_zwave_nodedefs("", family_id="4", instance_id="1") == {}
    assert parse_zwave_nodedefs("  ", family_id="4", instance_id="1") == {}
    assert parse_zwave_nodedefs("<nodeDefs/>", family_id="4", instance_id="1") == {}


def test_parse_zwave_nodedefs_malformed_xml_raises() -> None:
    with pytest.raises(ClientError, match="Z-Wave nodedefs"):
        parse_zwave_nodedefs("<nodeDefs><nodedef", family_id="4", instance_id="1")
