"""Tests for the JSON + XML parsers in :mod:`pyisyox.client`."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pyisyox.client import (
    ClientError,
    NodePropertyValue,
    _unwrap_data,
    merge_status_into_nodes,
    parse_api_nodes,
    parse_api_programs,
    parse_rest_networking_resources,
    parse_rest_status,
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


def test_parse_api_nodes_handles_empty_payload() -> None:
    assert parse_api_nodes({}) == {}
    assert parse_api_nodes({"data": {}}) == {}
    assert parse_api_nodes({"data": {"nodes": {}}}) == {}


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


def test_parse_rest_networking_resources_raises_on_malformed_xml() -> None:
    with pytest.raises(ClientError):
        parse_rest_networking_resources("<not really xml")


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
