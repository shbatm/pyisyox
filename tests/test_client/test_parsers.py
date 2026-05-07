"""Tests for the JSON + XML parsers in :mod:`pyisyox.client`."""

from __future__ import annotations

import pytest

from pyisyox.client import (
    ClientError,
    NodePropertyValue,
    _unwrap_data,
    merge_status_into_nodes,
    parse_api_nodes,
    parse_rest_status,
)

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
