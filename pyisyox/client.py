"""JSON-first HTTP client for IoX 6+ controllers.

Sits between the auth layer (:mod:`pyisyox.auth`) and the schema layer
(:mod:`pyisyox.schema`) and orchestrates the initial connect:

* ``GET /api/config`` — uuid, version, portalHost (small, prerequisite
  for anything else).
* ``GET /rest/profiles?include=nodedefs,editors,linkdefs`` — single
  ~117 KB JSON blob; the source for every nodedef + editor.
* Parallel fan-out:
    * ``GET /api/nodes`` — JSON structure (family/instance, addresses,
      parent/pnode). Plugin nodes have **no** ``property[]`` field.
    * ``GET /rest/status`` — XML, the canonical full-property table for
      both native and plugin nodes. PyISY 3.x already merges this into
      ``/rest/nodes`` to fill in Insteon thermostat properties; the
      same merge step handles plugin nodes uniformly here.
    * ``GET /api/programs``, ``/api/triggers`` — JSON.
    * ``GET /api/variables/1`` and ``/api/variables/2`` — JSON.

Total: ≤ 7 HTTP + 1 WebSocket (phase 3b lands the HTTP half; WebSocket
attaches later in phase 4 wiring).

The client is auth-mode-agnostic — it accepts any :class:`pyisyox.auth.Auth`
implementation (``PortalAuth`` or ``LocalAuth``). On a 401 it asks the
auth strategy to recover, retrying the original request once if recovery
succeeds.

XML decoders here are deliberately narrow — the only legacy XML surfaces
left after the JSON-first cut are ``/rest/status`` (used here),
``/rest/nodes/{addr}/cmd/...`` responses (touched at command-send time),
and ``/rest/subscribe`` event frames (handled by the WebSocket pipeline).
``xml.etree.ElementTree`` from the stdlib covers all three; the
``xmltodict`` runtime dep is dropped in phase 3c.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from xml.etree import ElementTree as ET

import aiohttp

from pyisyox.auth import Auth, AuthError
from pyisyox.redactor import redact_sensitive
from pyisyox.schema import Profile

_LOGGER = logging.getLogger(__name__)


class ClientError(Exception):
    """Base error for client-level failures (HTTP non-2xx, parse errors)."""


class HTTPError(ClientError):
    """Non-2xx response after auth retries are exhausted."""

    def __init__(self, status: int, url: str) -> None:
        """Capture the failing status code and URL."""
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.url = url


@dataclass(slots=True, frozen=True)
class ControllerConfig:
    """Subset of ``/api/config`` that the rest of the load flow needs."""

    uuid: str
    version: str
    portal_host: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class NodePropertyValue:
    """One live property value, normalised to a single shape regardless of
    whether it arrived from ``/api/nodes`` JSON or ``/rest/status`` XML.

    The shape mirrors :class:`pyisyox.schema.nodedef.Property` but is kept
    here as a private data carrier so the client can produce them without
    importing the runtime Node classes (which arrive in phase 4).
    """

    id: str
    value: str
    formatted: str = ""
    uom: str = ""
    name: str = ""


@dataclass(slots=True)
class NodeRecord:
    """One node from ``/api/nodes``, with property values merged in from
    ``/rest/status``. The structural fields come from JSON; the
    ``properties`` dict is the merged-in canonical state.
    """

    address: str
    name: str
    nodedef_id: str
    family_id: str
    instance_id: str
    type: str = ""
    parent_address: str | None = None
    pnode: str | None = None
    enabled: bool = True
    properties: dict[str, NodePropertyValue] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class LoadResult:
    """Output of :meth:`IoXClient.connect`.

    Attributes:
        config: Parsed ``/api/config`` slice.
        profile: Decoded ``/rest/profiles`` blob, ready for nodedef
            lookups via ``profile.find_nodedef(...)``.
        nodes: Map of address → :class:`NodeRecord` with merged properties.
        programs: Raw ``/api/programs`` ``data`` payload (typed wrappers
            arrive in phase 4).
        triggers: Raw ``/api/triggers`` payload — the program AST as JSON.
        variables: Map of variable type id (``"1"`` or ``"2"``) to the
            raw ``/api/variables/{type}`` ``data`` list.
    """

    config: ControllerConfig
    profile: Profile
    nodes: dict[str, NodeRecord]
    programs: list[dict[str, Any]]
    triggers: list[dict[str, Any]]
    variables: dict[str, list[dict[str, Any]]]


class IoXClient:
    """Auth-aware async HTTP client for IoX 6+ controllers."""

    def __init__(self, base_url: str, auth: Auth, session: aiohttp.ClientSession) -> None:
        """Initialise the client.

        Args:
            base_url: Scheme + host + port — e.g. ``"https://eisy.local:443"``
                for portal mode or ``"https://eisy.local:8443"`` for local.
                No trailing slash.
            auth: Either :class:`PortalAuth` or :class:`LocalAuth`.
            session: An aiohttp ``ClientSession`` the client will use for
                every request. The caller owns the session lifecycle.
        """
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.session = session
        self._authenticated = False

    async def connect(self) -> LoadResult:
        """Authenticate (if needed) and run the parallel initial load.

        Order:
            1. ``GET /api/config`` — synchronous, must succeed before the
               rest of the calls fire.
            2. Authenticate via the auth strategy (no-op for LocalAuth).
            3. Parallel: profiles, nodes, status, programs, triggers,
               variables/1, variables/2.
            4. Merge ``/rest/status`` properties into the node records.

        Returns:
            A populated :class:`LoadResult`.
        """
        config = await self._fetch_config()
        await self._authenticate_once()

        (
            profile_raw,
            nodes_raw,
            status_xml,
            programs_raw,
            triggers_raw,
            vars_int_raw,
            vars_state_raw,
        ) = await asyncio.gather(
            self._get_json("/rest/profiles?include=nodedefs,editors,linkdefs"),
            self._get_json("/api/nodes"),
            self._get_text("/rest/status"),
            self._get_json("/api/programs"),
            self._get_json("/api/triggers"),
            self._get_json("/api/variables/1"),
            self._get_json("/api/variables/2"),
        )

        profile = Profile.load_from_json(profile_raw)
        nodes = parse_api_nodes(nodes_raw)
        merge_status_into_nodes(nodes, parse_rest_status(status_xml))

        return LoadResult(
            config=config,
            profile=profile,
            nodes=nodes,
            programs=_unwrap_data(programs_raw),
            triggers=_unwrap_data(triggers_raw),
            variables={
                "1": _unwrap_data(vars_int_raw),
                "2": _unwrap_data(vars_state_raw),
            },
        )

    async def _fetch_config(self) -> ControllerConfig:
        """``GET /api/config`` — minimal, used to confirm IoX 6+ + uuid."""
        raw = await self._get_json("/api/config", authenticated=False)
        data = raw.get("data", raw)
        return ControllerConfig(
            uuid=str(data.get("uuid", "")),
            version=str(data.get("version", "")),
            portal_host=data.get("portalHost"),
            raw=data,
        )

    async def _authenticate_once(self) -> None:
        if self._authenticated:
            return
        await self.auth.authenticate(self.session, self.base_url)
        self._authenticated = True

    async def _get_json(self, path: str, *, authenticated: bool = True) -> Any:
        """GET a JSON endpoint. Applies auth (if requested) and retries
        once on 401 if the auth strategy can recover."""
        text = await self._get_text(path, authenticated=authenticated)
        try:
            payload = _loads_json(text)
        except ValueError as exc:
            raise ClientError(f"invalid JSON from {path}: {exc}") from exc
        if _LOGGER.isEnabledFor(logging.DEBUG):
            _LOGGER.debug("GET %s -> %s", path, redact_sensitive(payload))
        return payload

    async def _get_text(self, path: str, *, authenticated: bool = True) -> str:
        """GET a text endpoint (used for XML responses)."""
        url = f"{self.base_url}{path}"
        attempt = 0
        while True:
            kwargs: dict[str, Any] = {}
            if authenticated:
                if not self._authenticated:
                    await self._authenticate_once()
                kwargs.update(await self.auth.request_kwargs(self.session, self.base_url))
            async with self.session.get(url, **kwargs) as resp:
                if resp.status == 401 and authenticated and attempt == 0:
                    if not await self.auth.handle_unauthorized(self.session, self.base_url):
                        raise AuthError(f"auth could not recover from 401 on {url}")
                    attempt += 1
                    continue
                if resp.status >= 400:
                    raise HTTPError(resp.status, url)
                return await resp.text()


# --- parsers --------------------------------------------------------------


def parse_api_nodes(raw: dict[str, Any]) -> dict[str, NodeRecord]:
    """Decode the ``/api/nodes`` JSON payload into a map of address → record.

    The wire shape is double-nested as ``data.nodes.node[]`` (preserved
    from the legacy XML element layout). Plugin nodes carry no
    ``property[]`` field — those are filled in by
    :func:`merge_status_into_nodes`.
    """
    nodes_data = (raw.get("data") or {}).get("nodes") or {}
    raw_list = nodes_data.get("node") or []
    out: dict[str, NodeRecord] = {}
    for item in raw_list:
        record = _node_from_api_json(item)
        out[record.address] = record
    return out


def _node_from_api_json(item: dict[str, Any]) -> NodeRecord:
    """Translate one ``/api/nodes`` element into a :class:`NodeRecord`.

    ``family`` arrives as either ``None`` (native, family/instance default
    to ``"1"``) or ``{"_": "<id>", "instance": "<inst>"}``.
    """
    family = item.get("family")
    if isinstance(family, dict):
        family_id = str(family.get("_", "1"))
        instance_id = str(family.get("instance", family_id))
    else:
        family_id = "1"
        instance_id = "1"

    parent = item.get("parent")
    parent_address = parent.get("_") if isinstance(parent, dict) else parent

    properties: dict[str, NodePropertyValue] = {}
    for prop in item.get("property") or []:
        if not isinstance(prop, dict) or "id" not in prop:
            continue
        properties[prop["id"]] = NodePropertyValue(
            id=prop["id"],
            value=str(prop.get("value", "")),
            formatted=str(prop.get("formatted", "")),
            uom=str(prop.get("uom", "")),
            name=str(prop.get("name", "")),
        )

    return NodeRecord(
        address=str(item["address"]),
        name=str(item.get("name", "")),
        nodedef_id=str(item.get("nodeDefId", "")),
        family_id=family_id,
        instance_id=instance_id,
        type=str(item.get("type", "")),
        parent_address=parent_address,
        pnode=item.get("pnode"),
        enabled=str(item.get("enabled", "true")).lower() == "true",
        properties=properties,
        raw=item,
    )


def parse_rest_status(xml: str) -> dict[str, dict[str, NodePropertyValue]]:
    """Decode ``/rest/status`` XML into ``{address: {prop_id: Property}}``.

    The shape is a flat ``<nodes><node id="..."><property id="..."
    value="..." formatted="..." uom="..." name=""/>...</node>...</nodes>``.
    Empty values (``value=""``) are preserved — callers should treat them
    as "controller has no value yet" rather than dropping the property.
    """
    if not xml:
        return {}
    try:
        root = ET.fromstring(xml)  # noqa: S314 — eisy is a trusted LAN device
    except ET.ParseError as exc:
        raise ClientError(f"failed to parse /rest/status XML: {exc}") from exc

    out: dict[str, dict[str, NodePropertyValue]] = {}
    for node in root.findall("node"):
        addr = node.get("id")
        if not addr:
            continue
        props: dict[str, NodePropertyValue] = {}
        for prop in node.findall("property"):
            pid = prop.get("id")
            if not pid:
                continue
            props[pid] = NodePropertyValue(
                id=pid,
                value=prop.get("value", ""),
                formatted=prop.get("formatted", ""),
                uom=prop.get("uom", ""),
                name=prop.get("name", ""),
            )
        out[addr] = props
    return out


def merge_status_into_nodes(
    nodes: dict[str, NodeRecord], status: dict[str, dict[str, NodePropertyValue]]
) -> None:
    """Overlay ``/rest/status`` properties onto each :class:`NodeRecord`.

    The merge always treats ``/rest/status`` as authoritative — both
    native nodes (where Insteon thermostats omit CLISPC/CLISPH/CLIMD/
    CLIHCS from ``/api/nodes``) and plugin nodes (which carry no
    ``property[]`` field at all). Status properties replace any existing
    JSON-side properties of the same id; status-only properties are
    inserted; properties present only in the JSON tree are kept.
    """
    for addr, node in nodes.items():
        for pid, prop in status.get(addr, {}).items():
            node.properties[pid] = prop


# --- private helpers ------------------------------------------------------


def _unwrap_data(raw: Any) -> list[dict[str, Any]]:
    """Pull the ``data`` array from a ``{successful, data: [...]}`` envelope.

    The eisy ``/api/*`` JSON endpoints all return that envelope. Returns
    an empty list when ``data`` is missing or not a list, so callers can
    rely on a stable shape even when an endpoint is empty.
    """
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, list):
            return data
    return []


def _loads_json(text: str) -> Any:
    """Local alias for json.loads — kept as a thin wrapper so tests can
    monkey-patch one symbol if they need to inject decode failures."""
    return json.loads(text)
