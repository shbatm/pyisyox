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
from pyisyox.client import ClientError, ControllerConfig, HTTPError, IoXClient
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
    # ``/api/config`` is auth-gated on both modes — the local-credentials
    # flow 401'd when this was fetched unauthenticated.
    _, path, kwargs = session.calls[0]
    assert path == "/api/config"
    assert kwargs.get("auth") is not None  # BasicAuth from LocalAuth was attached


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
    session.set_route("GET", "/api/programs", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/triggers", 200, {"successful": True, "data": []})
    session.set_route(
        "GET", "/api/variables/1", 200, {"successful": True, "data": [{"id": "1", "name": "X"}]}
    )
    session.set_route("GET", "/api/variables/2", 200, {"successful": True, "data": []})
    session.set_route(
        "GET",
        "/rest/networking/resources",
        200,
        '<?xml version="1.0"?><NetConfig><NetRule><id>1</id><name>Reboot Router</name></NetRule></NetConfig>',
    )
    # Scripted explicitly even though FakeSession defaults /api/groups —
    # this full-load test intentionally pins every fan-out route so the
    # call-count assertion below is self-contained.
    session.set_route("GET", "/api/groups", 200, {"successful": True, "data": {"groups": []}})

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

    # Variables parsed into typed records keyed by id within type.
    assert result.variables["1"]["1"].name == "X"
    assert result.variables["2"] == {}

    # Networking module surfaced
    assert list(result.network_resources) == ["1"]
    assert result.network_resources["1"].name == "Reboot Router"

    # Total HTTP cost: 1 (config setup) + 1 (login setup) + 9 (parallel
    # fan-out) = 11 calls. The fan-out covers: profiles, /api/nodes
    # (which now carries groups + folders too, see #127), /rest/status,
    # /api/programs, /api/triggers, /api/variables/1, /api/variables/2,
    # /rest/networking/resources, /api/groups (link-target enrichment).
    # Still constant w.r.t. node-server count.
    fanout = [c for c in session.calls if c[0] == "GET" and c[1] not in ("/api/config",)]
    assert len(fanout) == 9


def _empty_fanout_routes(session: FakeSession) -> None:
    """Script the seven non-/api/nodes fan-out endpoints with empty
    payloads — enough for ``load()`` to complete; tests override the
    pieces they care about afterwards."""
    profile_json = json.loads((FIXTURE_DIR / "profiles_with_flume.json").read_text())
    session.set_route("GET", "/rest/profiles?include=nodedefs,editors,linkdefs", 200, profile_json)
    session.set_route("GET", "/rest/status", 200, "<nodes/>")
    session.set_route("GET", "/api/programs", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/triggers", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/variables/1", 200, {"successful": True, "data": []})
    session.set_route("GET", "/api/variables/2", 200, {"successful": True, "data": []})
    session.set_route("GET", "/rest/networking/resources", 404)
    session.set_route("GET", "/api/groups", 200, {"successful": True, "data": {"groups": []}})


@pytest.mark.asyncio
async def test_load_fetches_and_merges_dynamic_zwave_nodedefs(session: FakeSession) -> None:
    """A Z-Wave node's ``UZW*`` nodedef isn't in ``/rest/profiles``, so
    ``load()`` GETs ``/rest/zwave/node/0/def/get`` and merges the parsed
    nodedefs into the live profile — the node's lookup then resolves."""
    _empty_fanout_routes(session)
    session.set_route(
        "GET",
        "/api/nodes",
        200,
        {
            "data": {
                "nodes": {
                    "node": [
                        {
                            "address": "ZW003_1",
                            "name": "ZW Relay",
                            "nodeDefId": "UZW0015",
                            "family": "4",
                            "type": "4.16.1.0",
                            "enabled": "true",
                            "pnode": "ZW003_1",
                        }
                    ]
                }
            }
        },
    )
    session.set_route(
        "GET",
        "/rest/zwave/node/0/def/get",
        200,
        (FIXTURE_DIR / "rest_zwave_nodedefs.xml").read_text(),
    )
    session.set_route(
        "GET",
        "/rest/profiles/family/-1/profile/1/download/nls/en_US.txt",
        200,
        "# global\nCMD-DON-NAME = On\nCMD-FDUP-NAME = Fade Up\n",
    )
    session.set_route(
        "GET",
        "/rest/profiles/family/4/profile/1/download/nls/en_US.txt",
        200,
        "ST-ST-NAME = Status\n",
    )

    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True  # skip the connect-time handshake
    result = await client.load(config=ControllerConfig(uuid="u", version="6.0.0"))

    nd = result.profile.find_nodedef("UZW0015", "4", "1")
    assert nd is not None
    assert any(c.id == "DON" for c in nd.cmds.accepts)
    assert ("GET", "/rest/zwave/node/0/def/get", {}) in [(m, p, {}) for m, p, _ in session.calls]
    # NLS labels merged onto the dynamically-loaded nodedef + its commands.
    assert result.profile.nls.command_name("FDUP") == "Fade Up"
    don = next(c for c in nd.cmds.accepts if c.id == "DON")
    assert don.name == "On"
    if "ST" in nd.properties:
        assert nd.properties["ST"].name == "Status"


@pytest.mark.asyncio
async def test_load_skips_dynamic_zwave_fetch_when_no_zwave_nodes(session: FakeSession) -> None:
    """No family-4/12 nodes ⇒ the dynamic-nodedef endpoint is never hit."""
    _empty_fanout_routes(session)
    session.set_route("GET", "/api/nodes", 200, {"data": {"nodes": {"node": []}}})

    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True
    await client.load(config=ControllerConfig(uuid="u", version="6.0.0"))

    assert not any("/zwave/" in p for _, p, _ in session.calls)


# --- Variable CRUD -------------------------------------------------------


@pytest.mark.asyncio
async def test_create_variable_puts_with_name_and_returns_envelope(session: FakeSession) -> None:
    """``PUT /api/variables/{type}`` carries ``{name}`` (no prec when
    default 0); response envelope is returned verbatim."""
    session.set_route(
        "PUT",
        "/api/variables/1",
        200,
        {"successful": True, "data": {"id": "3", "name": "New Int Var", "prec": 0}},
    )
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    response = await client.create_variable("1", "New Int Var")

    method, path, kwargs = session.calls[-1]
    assert (method, path) == ("PUT", "/api/variables/1")
    assert kwargs["json"] == {"name": "New Int Var"}
    assert response["data"]["id"] == "3"


@pytest.mark.asyncio
async def test_create_variable_includes_prec_when_nonzero(session: FakeSession) -> None:
    session.set_route(
        "PUT",
        "/api/variables/2",
        200,
        {"successful": True, "data": {"id": "5", "name": "X", "prec": 2}},
    )
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    await client.create_variable("2", "X", prec=2)

    _, _, kwargs = session.calls[-1]
    assert kwargs["json"] == {"name": "X", "prec": 2}


@pytest.mark.asyncio
async def test_create_variable_raises_on_empty_response_body(session: FakeSession) -> None:
    """Defensive: a PUT with no body would otherwise return ``None``
    and downstream consumers would crash on ``response['data']``."""
    session.set_route("PUT", "/api/variables/1", 200, None)
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    with pytest.raises(Exception, match="empty response body"):
        await client.create_variable("1", "X")


@pytest.mark.asyncio
async def test_delete_variable_hits_delete_endpoint_and_tolerates_empty_body(
    session: FakeSession,
) -> None:
    """DELETE responses commonly carry no body; ``_send_json`` returns
    ``None`` and ``delete_variable`` drops it."""
    session.set_route("DELETE", "/api/variables/2/8", 200, None)
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    await client.delete_variable("2", "8")

    method, path, _ = session.calls[-1]
    assert (method, path) == ("DELETE", "/api/variables/2/8")


@pytest.mark.asyncio
async def test_delete_variable_accepts_envelope_body(session: FakeSession) -> None:
    """When the controller does include the ``{successful, data: null}``
    envelope, ``_send_json`` parses it and ``delete_variable`` still
    resolves cleanly."""
    session.set_route(
        "DELETE",
        "/api/variables/2/8",
        200,
        {"successful": True, "data": None},
    )
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    await client.delete_variable("2", "8")


@pytest.mark.asyncio
async def test_get_variables_type_returns_parsed_records(session: FakeSession) -> None:
    """``get_variables_type`` is the thin GET+parse wrapper the
    controller uses for ``refresh_variables``. Asserting it produces
    typed records keeps controller-side tests focused on the in-place
    update behavior rather than re-asserting the parser shape."""
    session.set_route(
        "GET",
        "/api/variables/1",
        200,
        {
            "successful": True,
            "data": [
                {"id": "5", "name": "Mode", "val": 3, "init": 0, "prec": 1, "ts": ""},
            ],
        },
    )
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    out = await client.get_variables_type("1")

    assert "5" in out
    assert out["5"].name == "Mode"
    assert out["5"].value == 3
    assert out["5"].precision == 1


@pytest.mark.asyncio
async def test_send_json_rejects_unsupported_method(session: FakeSession) -> None:
    """Passing a method outside the allowlist raises ``ValueError``
    rather than silently dispatching to a different session attribute
    via ``getattr``."""
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    with pytest.raises(ValueError, match="unsupported _send_json method"):
        await client._send_json("PATCH", "/anywhere", {"x": 1})  # type: ignore[arg-type]


# --- run_program_command ---------------------------------------------------


@pytest.mark.asyncio
async def test_run_program_command_sends_id_verbatim(session: FakeSession) -> None:
    """``run_program_command`` no longer converts anything -- callers
    (``Program`` / ``ProgramFolder``) always hold the classic hex id
    off ``ProgramRecord.address``, which ``parse_api_programs``
    upconverts once at parse time (#193); this method just formats it
    into the URL."""
    session.set_route("GET", "/rest/programs/0095/runElse", 200, "<RestResponse status='200'/>")
    client = IoXClient(BASE, LocalAuth("admin", "p"), session)  # type: ignore[arg-type]
    client._authenticated = True

    await client.run_program_command("0095", "runElse")

    _, path, _ = session.calls[-1]
    assert path == "/rest/programs/0095/runElse"
