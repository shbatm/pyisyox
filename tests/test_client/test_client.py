"""Tests for :class:`pyisyox.client.IoXClient` — auth retry, parallel
load orchestration, and the JSON+XML round-trip against scripted responses."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from typing import Any

import pytest

from pyisyox.auth import AuthError, LocalAuth, PortalAuth
from pyisyox.client import ClientError, HTTPError, IoXClient
from tests.test_client.conftest import FakeSession

BASE = "https://eisy.local"
FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "eisy6"


# --- ControllerConfig + auth handshake -----------------------------------


@pytest.mark.asyncio
async def test_fetch_config_unwraps_data_envelope(session: FakeSession) -> None:
    session.set_route(
        "GET",
        "/api/config",
        200,
        {"successful": True, "data": {"uuid": "00:21:b9:f2:72:65", "version": "6.0.0"}},
    )
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    cfg = await client._fetch_config()
    assert cfg.uuid == "00:21:b9:f2:72:65"
    assert cfg.version == "6.0.0"


@pytest.mark.asyncio
async def test_get_text_attaches_local_auth(session: FakeSession) -> None:
    session.set_route("GET", "/rest/status", 200, "<nodes/>")
    client = IoXClient(BASE, LocalAuth("admin", "pw"), session)  # type: ignore[arg-type]
    await client._authenticate_once()
    text = await client._get_text("/rest/status")
    assert text == "<nodes/>"
    method, path, kwargs = session.calls[0]
    assert method == "GET" and path == "/rest/status"
    assert kwargs.get("auth") is not None  # BasicAuth from LocalAuth


# --- 401 retry ------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_text_retries_once_on_401_when_auth_recovers(session: FakeSession) -> None:
    """First GET returns 401; auth.handle_unauthorized refreshes; the
    client retries and gets the real 200."""
    sess = session
    sess.queue("GET", "/api/nodes", 401)
    sess.queue("GET", "/api/nodes", 200, {"data": {"nodes": {"node": []}}})

    # Portal flow: login + (proactive refresh path not triggered) + refresh after 401
    sess.queue("POST", "/api/login", 200, _login_body())
    sess.queue("POST", "/api/jwt/refresh", 200, _login_body())

    auth = PortalAuth("u@x", "p")
    client = IoXClient(BASE, auth, sess)  # type: ignore[arg-type]
    await client._authenticate_once()

    text = await client._get_text("/api/nodes")
    assert text  # non-empty 200 body
    method_path = [(m, p) for m, p, _ in sess.calls]
    # login (handshake) -> nodes (401) -> refresh (recovery) -> nodes (retry 200)
    assert method_path == [
        ("POST", "/api/login"),
        ("GET", "/api/nodes"),
        ("POST", "/api/jwt/refresh"),
        ("GET", "/api/nodes"),
    ]


@pytest.mark.asyncio
async def test_get_text_propagates_when_auth_cannot_recover(session: FakeSession) -> None:
    session.set_route("GET", "/rest/status", 401)
    client = IoXClient(BASE, LocalAuth("admin", "wrong"), session)  # type: ignore[arg-type]
    await client._authenticate_once()
    with pytest.raises(AuthError, match="could not recover"):
        await client._get_text("/rest/status")


@pytest.mark.asyncio
async def test_get_text_raises_httperror_on_5xx(session: FakeSession) -> None:
    session.set_route("GET", "/api/nodes", 503)
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    await client._authenticate_once()
    with pytest.raises(HTTPError) as exc:
        await client._get_text("/api/nodes")
    assert exc.value.status == 503


@pytest.mark.asyncio
async def test_get_json_raises_clienterror_on_invalid_json(session: FakeSession) -> None:
    session.set_route("GET", "/api/config", 200, "not json {")
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    with pytest.raises(ClientError, match="invalid JSON"):
        await client._get_json("/api/config", authenticated=False)


# --- end-to-end connect() ------------------------------------------------


def _login_body() -> dict[str, Any]:
    """Reuse the same shape PortalAuth tests use: nested data with valid JWT
    expiries far in the future."""

    def _jwt(exp: float) -> str:
        def b64(d: dict) -> str:
            return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

        return f"{b64({'alg': 'ES256'})}.{b64({'exp': exp})}.sig"

    in_one_hour = time.time() + 3600
    in_thirty_days = time.time() + 30 * 86400
    return {
        "successful": True,
        "data": {
            "accessToken": _jwt(in_one_hour),
            "refreshToken": _jwt(in_thirty_days),
            "ssl": {"key": "PRIVATE", "cert": "...", "ca": "..."},
        },
    }


@pytest.mark.asyncio
async def test_connect_runs_full_load_with_real_profile_fixture(session: FakeSession) -> None:
    """Wire up scripted responses for every endpoint connect() touches and
    verify the end-to-end flow lands a Profile + merged NodeRecords."""
    profile_json = json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())

    # Native (Insteon) node arriving with property[]; plugin (Flume) without.
    nodes_payload = {
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
                    },
                    {
                        "address": "n010_84dd4c2c24c3b7",
                        "name": "Flume Sensor",
                        "nodeDefId": "flume2",
                        "family": {"_": "10", "instance": "10"},
                        "type": "1.2.3.4",
                        "enabled": "true",
                        "parent": {"_": "n010_controller", "type": "1"},
                        "pnode": "n010_controller",
                    },
                ]
            }
        }
    }
    status_xml = (
        '<?xml version="1.0" encoding="UTF-8"?><nodes>'
        '<node id="3D 7D 87 1">'
        '<property id="ST" value="255" formatted="On" uom="100" name=""/>'
        "</node>"
        '<node id="n010_84dd4c2c24c3b7">'
        '<property id="ST" value="1" formatted="True" uom="2"/>'
        '<property id="GV1" value="6839" formatted="0.6839 US gallons" uom="69"/>'
        "</node>"
        "</nodes>"
    )

    session.set_route(
        "GET", "/api/config", 200, {"successful": True, "data": {"uuid": "u", "version": "6.0.0"}}
    )
    session.set_route("POST", "/api/login", 200, _login_body())
    session.set_route("GET", "/rest/profiles?include=nodedefs,editors,linkdefs", 200, profile_json)
    session.set_route("GET", "/api/nodes", 200, nodes_payload)
    session.set_route("GET", "/rest/status", 200, status_xml)
    session.set_route("GET", "/rest/nodes", 200, '<?xml version="1.0"?><nodes><root/></nodes>')
    session.set_route("GET", "/api/programs", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/triggers", 200, {"successful": True, "data": []})
    session.set_route(
        "GET", "/api/variables/1", 200, {"successful": True, "data": [{"id": "1", "name": "X"}]}
    )
    session.set_route("GET", "/api/variables/2", 200, {"successful": True, "data": []})

    client = IoXClient(BASE, PortalAuth("u@example.com", "pass"), session)  # type: ignore[arg-type]
    result = await client.connect()

    # Schema decoded
    assert result.profile.find_nodedef("flume2", "10", "10") is not None
    assert result.profile.find_nodedef("KeypadDimmer_ADV", "1", "1") is not None

    # Native: status overrides JSON property (ST went from 0/Off to 255/On)
    assert result.nodes["3D 7D 87 1"].properties["ST"].formatted == "On"

    # Plugin: status fills the empty /api/nodes property dict
    flume = result.nodes["n010_84dd4c2c24c3b7"]
    assert set(flume.properties) == {"ST", "GV1"}
    assert flume.properties["GV1"].formatted == "0.6839 US gallons"
    assert flume.family_id == "10"

    # Variables data unwrapped
    assert result.variables["1"][0]["name"] == "X"
    assert result.variables["2"] == []

    # Total HTTP cost: 1 (config setup) + 1 (login setup) + 8 (parallel
    # fan-out) = 10 calls. The fan-out covers: profiles, /api/nodes,
    # /rest/nodes (groups + folders), /rest/status, /api/programs,
    # /api/triggers, /api/variables/1, /api/variables/2. The original
    # "<=7" target predated /rest/nodes; it grew to 8 once group + folder
    # support landed. Still constant w.r.t. node-server count.
    fanout = [c for c in session.calls if c[0] == "GET" and c[1] not in ("/api/config",)]
    assert len(fanout) == 8
