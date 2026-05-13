"""Tests for the ``to_dict`` methods on the runtime wrappers.

Each runtime class exposes ``to_dict()`` returning a JSON-compatible
dict (the v1-beta dumper resurrection). The implementations all walk
their underlying record via :func:`dataclasses.asdict`; tests pin the
shape, the JSON round-trip, and the derived-field additions so a
future refactor of any one class doesn't silently break the dumper.
"""

from __future__ import annotations

import json
from pathlib import Path

import aiohttp
import pytest

from pyisyox.auth import LocalAuth
from pyisyox.client import (
    FolderRecord,
    GroupRecord,
    IoXClient,
    NetworkResourceRecord,
    NodePropertyValue,
    NodeRecord,
    ProgramRecord,
    VariableRecord,
)
from pyisyox.controller import Controller, ControllerNotConnectedError
from pyisyox.runtime import (
    Folder,
    Group,
    NetworkResource,
    Node,
    Program,
    ProgramFolder,
    Variable,
)
from pyisyox.schema.profile import Profile
from tests.test_client.conftest import FakeSession
from tests.test_controller import FakeSession as CombinedFakeSession
from tests.test_controller import _stub_responses
from tests.test_runtime.test_ws import FakeWSMessage

FIXTURE_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "eisy6"
BASE = "http://eisy.local:8080"


def _profile() -> Profile:
    raw = json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())
    return Profile.load_from_json(raw)


def _client() -> IoXClient:
    return IoXClient(BASE, LocalAuth("admin", "p"), FakeSession(BASE))  # type: ignore[arg-type]


# --- Folder --------------------------------------------------------------


def test_folder_to_dict_mirrors_record_fields() -> None:
    record = FolderRecord(address="10", name="Entry", family_id="13")
    payload = Folder(record).to_dict()
    assert payload == {
        "address": "10",
        "name": "Entry",
        "family_id": "13",
        "parent_address": None,
    }


# --- NetworkResource -----------------------------------------------------


def test_network_resource_to_dict_round_trips_json() -> None:
    record = NetworkResourceRecord(address="5", name="Doorbell")
    payload = NetworkResource(record, _client()).to_dict()
    assert payload == {"address": "5", "name": "Doorbell"}
    assert json.loads(json.dumps(payload)) == payload


# --- Variable ------------------------------------------------------------


def test_variable_to_dict_includes_value_and_init() -> None:
    record = VariableRecord(
        type_id="1", id="3", name="Counter", value=42, init=0, precision=0
    )
    payload = Variable(record, _client()).to_dict()
    assert payload["type_id"] == "1"
    assert payload["id"] == "3"
    assert payload["name"] == "Counter"
    assert payload["value"] == 42
    assert payload["init"] == 0


# --- Group ---------------------------------------------------------------


def test_group_to_dict_adds_aggregate_flags() -> None:
    """``group_all_on`` / ``group_any_on`` are derived on access; they
    shouldn't live on the underlying record but must show up in the
    snapshot so the dumper output reflects the scene's live state."""
    record = GroupRecord(
        address="G1",
        name="Living Room",
        nodedef_id="InsteonDimmer",
        family_id="6",
        instance_id="1",
    )
    payload = Group.from_record(record, _profile(), _client(), nodes={}).to_dict()
    assert payload["address"] == "G1"
    assert payload["name"] == "Living Room"
    assert "group_all_on" in payload
    assert "group_any_on" in payload


# --- Program -------------------------------------------------------------


def test_program_to_dict_carries_status_and_runtime_fields() -> None:
    record = ProgramRecord(
        address="0030",
        name="Switch",
        path="/Programs/Switch",
        parent_address=None,
        is_folder=False,
        status=True,
        running=None,
        enabled=True,
        run_at_startup=False,
    )
    payload = Program(record, _client()).to_dict()
    assert payload["address"] == "0030"
    assert payload["status"] is True
    assert payload["enabled"] is True
    assert payload["is_folder"] is False


def test_program_folder_to_dict_inherits_from_base() -> None:
    """``ProgramFolder`` reuses ``_ProgramBase.to_dict`` — same record,
    just ``is_folder=True``."""
    record = ProgramRecord(
        address="0001",
        name="Container",
        path="/Programs/Container",
        parent_address=None,
        is_folder=True,
        status=False,
        running=None,
        enabled=True,
        run_at_startup=None,
    )
    payload = ProgramFolder(record, _client()).to_dict()
    assert payload["is_folder"] is True
    assert payload["address"] == "0001"


# --- Node ----------------------------------------------------------------


def test_node_to_dict_includes_derived_protocol() -> None:
    """``Node.protocol`` is derived from ``family_id``; expose it in the
    snapshot so the dumper carries the protocol classification without
    consumers having to re-derive it."""
    record = NodeRecord(
        address="3D 7D 87 1",
        name="Test",
        nodedef_id="KeypadDimmer_ADV",
        family_id="1",
        instance_id="1",
        type="1.65.69.0",
        properties={
            "ST": NodePropertyValue(id="ST", value="0", formatted="Off"),
        },
    )
    payload = Node.from_record(record, _profile(), _client()).to_dict()
    assert payload["address"] == "3D 7D 87 1"
    assert payload["protocol"] == "insteon"
    # Property values are themselves dataclasses — asdict walks recursively.
    assert payload["properties"]["ST"] == {
        "id": "ST",
        "value": "0",
        "formatted": "Off",
        "uom": "",
        "name": "",
        "precision": 0,
    }


def test_node_to_dict_json_round_trips() -> None:
    """``json.dumps`` over the snapshot must succeed — the whole point
    of the helper is to feed JSON-emitting consumers (HA diagnostics,
    file dumper). Pin this once at the node level."""
    record = NodeRecord(
        address="A 1",
        name="Test",
        nodedef_id="RelayLampOnly",
        family_id="1",
        instance_id="1",
        type="2.65.69.0",
    )
    payload = Node.from_record(record, _profile(), _client()).to_dict()
    assert json.loads(json.dumps(payload)) == payload


# --- Profile -------------------------------------------------------------


def test_profile_to_dict_drops_tuple_keyed_lookup() -> None:
    """``nodedef_lookup`` is ``(nodedef_id, family_id, instance_id)``-
    keyed; JSON can't encode tuple keys. Snapshot surfaces the size as
    a counter and keeps the families tree as the structural source."""
    profile = _profile()
    payload = profile.to_dict()
    assert "nodedef_lookup" not in payload
    assert payload["nodedef_lookup_count"] > 0
    assert isinstance(payload["families"], dict)
    assert "nls" in payload
    # And it survives a JSON round-trip.
    json.dumps(payload)


# --- ProgramFolder + Program from base path --------------------------------


# --- Controller aggregator ----------------------------------------------


@pytest.mark.asyncio
async def test_controller_to_dict_aggregates_collections() -> None:
    """``Controller.to_dict()`` is the public surface the file-dumper
    CLI builds on. It walks every loaded collection via the
    per-runtime ``to_dict`` methods, adds the controller's own
    config + WebSocket health, and survives ``json.dumps``."""
    session = CombinedFakeSession(BASE)
    _stub_responses(session)
    session.queue_ws([FakeWSMessage(type=aiohttp.WSMsgType.CLOSED)])
    controller = Controller(BASE, LocalAuth("admin", "p"), session=session)  # type: ignore[arg-type]

    # Pre-connect access raises rather than returning a half-snapshot.
    with pytest.raises(ControllerNotConnectedError):
        controller.to_dict()

    await controller.connect()
    try:
        payload = controller.to_dict()
    finally:
        await controller.stop()

    assert payload["connected"] is True
    assert payload["config"]["uuid"] == "uuid-1"
    assert payload["config"]["version"] == "6.0.0"
    assert "3D 7D 87 1" in payload["nodes"]
    assert isinstance(payload["profile"], dict)
    assert "websocket" in payload
    # JSON round-trip: catches any leaked set / tuple-key from the
    # nested per-object snapshots.
    assert json.loads(json.dumps(payload))["connected"] is True


@pytest.mark.parametrize("is_folder", [False, True])
def test_program_to_dict_round_trips_json(is_folder: bool) -> None:
    """Both Program and ProgramFolder snapshots are JSON-compatible."""
    record = ProgramRecord(
        address="0010",
        name="P",
        path="/Programs/P",
        parent_address=None,
        is_folder=is_folder,
        status=True,
        running=None,
        enabled=True,
    )
    klass: type = ProgramFolder if is_folder else Program
    payload = klass(record, _client()).to_dict()
    assert json.loads(json.dumps(payload)) == payload
