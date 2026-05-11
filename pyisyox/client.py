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

Total: ≤ 7 HTTP + 1 WebSocket regardless of node-server count.

The client is auth-mode-agnostic — it accepts any :class:`pyisyox.auth.Auth`
implementation (``PortalAuth`` or ``LocalAuth``). On a 401 it asks the
auth strategy to recover, retrying the original request once if recovery
succeeds.

XML decoders here are deliberately narrow — the only legacy XML surfaces
left after the JSON-first cut are ``/rest/status`` (used here),
``/rest/nodes/{addr}/cmd/...`` responses (touched at command-send time),
and ``/rest/subscribe`` event frames (handled by the WebSocket pipeline).
``xml.etree.ElementTree`` from the stdlib covers all three.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote
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


@dataclass(slots=True)
class NodePropertyValue:
    """One live property value, normalised to a single shape regardless of
    whether it arrived from ``/api/nodes`` JSON or ``/rest/status`` XML.

    The shape mirrors :class:`pyisyox.schema.nodedef.Property` but is kept
    here as a private data carrier so the client can produce values
    without importing the runtime Node classes.

    ``precision`` is the decimal precision the controller declares for
    this value (``raw / 10**precision`` is the displayed number). The
    wire keyed it as ``"prec"`` across all three ingest paths —
    ``/api/nodes`` JSON has ``"prec": <int>``, ``/rest/status`` XML uses
    ``prec="<int>"``, and WS frames put it on the ``<action prec="...">``
    element — but the Python attribute spells it out for readability
    (matches PyISY 3.x's ``precision`` naming). Defaults to ``0`` (= no
    scaling) when the controller omits it.
    """

    id: str
    value: str
    formatted: str = ""
    uom: str = ""
    name: str = ""
    precision: int = 0


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
    #: Bitfield from the controller's node table — see
    #: :class:`pyisyox.constants.NodeFlag` for the bit meanings (NEW,
    #: IN_ERR, DEVICE_ROOT, ...). Sourced from the ``flag`` field on
    #: ``/api/nodes`` JSON (which the controller stringifies — e.g.
    #: ``"128"``); ``0`` when the controller didn't supply one for
    #: this node.
    flag: int = 0
    properties: dict[str, NodePropertyValue] = field(default_factory=dict)


@dataclass(slots=True)
class GroupRecord:
    """One scene/group from ``/rest/nodes``.

    A group represents a controller-managed collection of nodes.
    Sending a command to ``address`` causes the controller to
    broadcast it to every entry in ``member_addresses``.

    Sourced from ``<group flag="132">`` elements in the legacy
    ``/rest/nodes`` XML. ``flag="12"`` (the special "ISY" group
    representing the controller itself) is filtered out at parse
    time and does not appear in the registry.
    """

    address: str
    name: str
    nodedef_id: str
    family_id: str
    instance_id: str = "1"
    parent_address: str | None = None
    pnode: str | None = None
    #: All member node addresses, in declaration order (controllers + responders).
    member_addresses: tuple[str, ...] = ()
    #: Subset of ``member_addresses`` whose ``<link type="16">`` flag marks them
    #: as scene controllers (rather than responders). Empty when the group has
    #: no explicit controller (e.g. SmartLinc-style virtual scenes).
    controller_addresses: tuple[str, ...] = ()


@dataclass(slots=True)
class FolderRecord:
    """One folder from ``/rest/nodes`` (organisational, no command surface).

    Folders use family ``"13"`` (folder family) on IoX. Sourced from
    ``<folder>`` elements.
    """

    address: str
    name: str
    family_id: str = "13"
    parent_address: str | None = None


@dataclass(slots=True)
class ProgramRecord:
    """One program from ``/api/programs``.

    Programs and program-folders live in the same flat list on the
    wire, discriminated by ``is_folder``. Folders carry only
    identity + status; programs additionally carry timing /
    enabled / running fields.

    The eisy returns ``status`` as the strings ``"true"`` or
    ``"false"`` (matching the legacy XML convention). They're
    decoded into a Python bool here. Empty time strings become
    ``None``.

    ``path`` is reconstructed from the ``parent_address`` chain at
    parse time so consumers can use the legacy ``HA.<platform>/...``
    folder convention without walking the tree themselves.

    Attributes:
        address: Program / folder id (4-character hex string).
        name: User-assigned label.
        path: Slash-joined ancestry, e.g. ``"My Programs/HA.switch/Foo/status"``.
            Excludes the root folder name ``"My Programs"`` to match
            the convention pyisy 3.x consumers used.
        parent_address: Parent folder id, or ``None`` for the root.
        is_folder: ``True`` for folders, ``False`` for programs.
        status: True when the program's last evaluation was True.
            Folder status reflects the AND of children. Empty string
            on the wire is treated as ``False``.
        enabled: ``False`` when the program is disabled. Always
            ``True`` for folders (which have no enabled flag on
            the wire). ``None`` if absent.
        run_at_startup: ``True`` if the program is set to run on
            controller boot. ``None`` for folders.
        running: Free-form runtime state — ``"idle"`` on idle
            programs; running programs report variants like
            ``"running then"`` / ``"running else"`` /
            ``"running if"``. ``None`` for folders.
        last_run_time / last_finish_time / next_scheduled_run_time:
            ISO 8601 timestamps as strings (``"2026-05-10T14:49:53.000Z"``)
            or ``None`` when absent. Kept as strings to avoid pulling
            in a datetime parser at this layer; consumers that need
            datetime objects can parse on read.
    """

    address: str
    name: str
    path: str
    parent_address: str | None
    is_folder: bool
    status: bool
    enabled: bool | None = None
    run_at_startup: bool | None = None
    running: str | None = None
    last_run_time: str | None = None
    last_finish_time: str | None = None
    next_scheduled_run_time: str | None = None


@dataclass(slots=True)
class VariableRecord:
    """One entry from ``/api/variables/{type}``.

    The IoX controller exposes two variable types — integer (``"1"``)
    and state (``"2"``) — and each carries an int value (``val``),
    init/restore-on-startup value, decimal precision, a user-assigned
    name, and a last-change timestamp. The wire JSON uses ``val`` for
    the current value; we expose it here as ``value`` so consumers
    don't have to track the wire spelling.

    Attributes:
        type_id: ``"1"`` (integer) or ``"2"`` (state).
        id: Variable id within the type.
        name: User-assigned label.
        value: Current value (wire field ``val``).
        init: Restore-on-startup value.
        precision: Decimal precision (``displayed = raw / 10**precision``).
            The wire keyed it as ``"prec"`` — Python attribute spells
            it out (matches PyISY 3.x).
        ts: Last-change timestamp as the controller emits it (ISO 8601
            UTC). Empty string when the controller omits it.
    """

    type_id: str
    id: str
    name: str
    value: int = 0
    init: int = 0
    precision: int = 0
    ts: str = ""

    @property
    def address(self) -> str:
        """``"{type_id}.{id}"`` — composite identifier that joins into
        controller endpoints and is useful for unique-id derivation in
        downstream consumers (HA entity unique ids, log lines, etc.)."""
        return f"{self.type_id}.{self.id}"


@dataclass(slots=True)
class NetworkResourceRecord:
    """One entry from ``/rest/networking/resources``.

    Network resources are user-defined HTTP / TCP / UDP fire-triggers
    on the controller (e.g. "ping the router", "post to a webhook").
    The runtime wrapper :class:`pyisyox.runtime.NetworkResource`
    surfaces ``run()`` which fires the resource by id.

    Attributes:
        address: Resource id, kept as a string for symmetry with
            node / group records (the wire is ``<id>5</id>``, an
            integer, but consumers want it joinable into URL paths).
        name: User-assigned label.
    """

    address: str
    name: str


@dataclass(slots=True)
class LoadResult:
    """Output of :meth:`IoXClient.connect`.

    Attributes:
        config: Parsed ``/api/config`` slice.
        profile: Decoded ``/rest/profiles`` blob, ready for nodedef
            lookups via ``profile.find_nodedef(...)``.
        nodes: Map of address → :class:`NodeRecord` with merged properties.
        groups: Map of address → :class:`GroupRecord` for IoX scenes.
        folders: Map of address → :class:`FolderRecord` for the
            organisational tree shown in the controller UI.
        programs: Map of id → :class:`ProgramRecord` for both
            programs and program-folders (discriminated by
            ``is_folder``). Empty when the controller has no
            programs configured.
        triggers: Raw ``/api/triggers`` payload — the program AST as JSON.
        variables: Map of variable type id (``"1"`` or ``"2"``) to the
            raw ``/api/variables/{type}`` ``data`` list.
        network_resources: Map of id → :class:`NetworkResourceRecord`,
            empty when the controller has no networking module
            enabled (the endpoint returns an empty ``<NetConfig/>``).
    """

    config: ControllerConfig
    profile: Profile
    nodes: dict[str, NodeRecord]
    groups: dict[str, GroupRecord]
    folders: dict[str, FolderRecord]
    programs: dict[str, ProgramRecord]
    triggers: list[dict[str, Any]]
    variables: dict[str, dict[str, VariableRecord]]
    network_resources: dict[str, NetworkResourceRecord]


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
        # Serialises authenticate() so concurrent first-use callers
        # collapse onto a single auth.authenticate() round-trip. Lazy
        # because asyncio.Lock construction needs a running loop.
        self._auth_lock: asyncio.Lock | None = None

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
        return await self.load(config)

    async def load(self, config: ControllerConfig | None = None) -> LoadResult:
        """Run the parallel load fan-out and return a fresh :class:`LoadResult`.

        Used both by :meth:`connect` (which prepends config + auth) and
        by :meth:`pyisyox.controller.Controller.refresh` (which re-runs
        the fan-out without re-authenticating).

        Args:
            config: Pre-fetched :class:`ControllerConfig` to attach to
                the returned LoadResult. When ``None``, the existing
                config is re-fetched (cheap — small JSON, no auth).

        Returns:
            A populated :class:`LoadResult`.
        """
        if config is None:
            config = await self._fetch_config()

        (
            profile_raw,
            nodes_raw,
            rest_nodes_xml,
            status_xml,
            programs_raw,
            triggers_raw,
            vars_int_raw,
            vars_state_raw,
            networking_xml,
        ) = await asyncio.gather(
            self._get_json("/rest/profiles?include=nodedefs,editors,linkdefs"),
            self._get_json("/api/nodes"),
            self._get_text("/rest/nodes"),
            self._get_text("/rest/status"),
            self._get_json("/api/programs"),
            self._get_json("/api/triggers"),
            self._get_json("/api/variables/1"),
            self._get_json("/api/variables/2"),
            # Networking module is optional — controllers without it
            # configured return an empty ``<NetConfig/>``. Tolerated by
            # the parser; we don't want a 404 here to abort load, so
            # we fall back to an empty document on HTTP errors.
            self._get_text_or_empty("/rest/networking/resources"),
        )

        profile = Profile.load_from_json(profile_raw)
        nodes = parse_api_nodes(nodes_raw)
        merge_status_into_nodes(nodes, parse_rest_status(status_xml))
        groups, folders = parse_rest_nodes_groups_folders(rest_nodes_xml)

        return LoadResult(
            config=config,
            profile=profile,
            nodes=nodes,
            groups=groups,
            folders=folders,
            programs=parse_api_programs(_unwrap_data(programs_raw, source="/api/programs")),
            triggers=_unwrap_data(triggers_raw, source="/api/triggers"),
            variables={
                "1": parse_api_variables_type(_unwrap_data(vars_int_raw, source="/api/variables/1"), "1"),
                "2": parse_api_variables_type(_unwrap_data(vars_state_raw, source="/api/variables/2"), "2"),
            },
            network_resources=parse_rest_networking_resources(networking_xml),
        )

    async def _fetch_config(self) -> ControllerConfig:
        """``GET /api/config`` — minimal, used to confirm IoX 6+ + uuid."""
        raw = await self._get_json("/api/config", authenticated=False)
        data = raw.get("data", raw)
        return ControllerConfig(
            uuid=str(data.get("uuid", "")),
            version=str(data.get("version", "")),
            portal_host=data.get("portalHost"),
        )

    async def _authenticate_once(self) -> None:
        """Run ``auth.authenticate`` exactly once across concurrent callers.

        The lock-then-recheck pattern collapses concurrent first-use
        callers onto a single ``auth.authenticate`` round-trip. Without
        it, two parallel requests during ``connect()`` setup could both
        observe ``_authenticated is False`` and each call ``authenticate``.
        """
        if self._authenticated:
            return
        if self._auth_lock is None:
            self._auth_lock = asyncio.Lock()
        async with self._auth_lock:
            # Double-checked: another coroutine may have authenticated
            # while we were queued for the lock. We deliberately re-read
            # via a local so mypy doesn't narrow it away as unreachable.
            already_authenticated: bool = self._authenticated
            if already_authenticated:
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

    async def _get_text_or_empty(self, path: str) -> str:
        """``_get_text`` that swallows HTTPError and returns an empty
        string. Used for optional-module endpoints (networking) where a
        missing module surfaces as a 404 / 503 rather than an empty
        document — we don't want those to abort initial load."""
        try:
            return await self._get_text(path)
        except HTTPError as exc:
            _LOGGER.debug("optional endpoint %s unavailable: %s", path, exc)
            return ""

    async def send_node_command(self, address: str, command_id: str, *params: int) -> str:
        """Issue ``GET /rest/nodes/{addr}/cmd/{cmd}[/{p1}[/{p2}...]]``.

        The legacy XML command surface is still the only command path
        on IoX 6 — no ``/api/*`` equivalent has surfaced in captures.
        Both auth modes (PortalAuth JWT, LocalAuth basic) accept this
        path. ``address`` is URL-quoted because Insteon addresses
        contain spaces (``"3D 7D 87 1"``).

        Args:
            address: Wire address of the target node.
            command_id: IoX command id (e.g. ``"DON"``).
            *params: Already-encoded integer parameters (the runtime
                :meth:`Node.send_command` runs the editor codec; this
                client method trusts its input).

        Returns:
            The text body of the response — typically a small
            ``<RestResponse status="200">...</RestResponse>`` envelope.
            Caller doesn't usually need to parse it; HTTPError covers
            non-2xx.
        """
        encoded_addr = quote(address, safe="")
        path_parts = [f"/rest/nodes/{encoded_addr}/cmd/{command_id}"]
        path_parts.extend(str(p) for p in params)
        path = "/".join(path_parts)
        return await self._get_text(path)

    async def post_variable_update(
        self, var_type: str | int, var_id: str | int, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Issue ``POST /api/variables/{type}/{id}`` with the supplied body.

        Three documented body shapes (verified against eisy-ui captures):

        * ``{"value": <int>}`` — set the current value
        * ``{"init": <int>}`` — set the initial / restore value
        * ``{"name": "<str>"}`` — rename the variable

        Mixing keys in one call wasn't observed; eisy-ui issues separate
        calls for each. Higher-level helpers in
        :class:`pyisyox.controller.Controller` enforce one-key-per-call
        for clarity.

        Returns the parsed response body (a ``{successful, data}``
        envelope).

        Raises:
            HTTPError on non-2xx; ClientError on malformed response.
        """
        return await self._post_json(f"/api/variables/{var_type}/{var_id}", body)

    async def run_program_command(self, program_id: str, command: str) -> str:
        """Send a program / folder command via the legacy REST endpoint.

        Wire shape: ``GET /rest/programs/{id}/{command}``. See
        :class:`pyisyox.runtime.ProgramCommand` for the typed
        command set; bare strings (the camelCase wire values) are
        accepted too.

        IoX 6 keeps this legacy path; no ``/api/programs/{id}/...``
        equivalent has been observed. The controller acknowledges
        receipt only — status changes flow back over the WebSocket.
        """
        return await self._get_text(f"/rest/programs/{program_id}/{command}")

    async def run_network_resource(self, resource_id: str | int) -> str:
        """Fire a network resource by id.

        Wire shape: ``GET /rest/networking/resources/{id}``. Response
        is a small ``<RestResponse status="200">`` envelope on success.
        The controller acknowledges receipt only — it doesn't return
        the result of the underlying HTTP / TCP / UDP fire.
        """
        return await self._get_text(f"/rest/networking/resources/{resource_id}")

    async def post_node_update(self, address: str, body: dict[str, Any]) -> dict[str, Any]:
        """Issue ``POST /api/nodes/{address}`` with the supplied body.

        Documented body shape (verified against eisy-ui capture):

        * ``{"name": "<str>", "nodeType": "node" | "group"}`` —
          rename the node or group. ``nodeType`` is required by the
          server even though the address already disambiguates.

        Returns the parsed response body (a ``{successful, data}``
        envelope).
        """
        encoded = quote(address, safe="")
        return await self._post_json(f"/api/nodes/{encoded}", body)

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Shared POST-JSON path with auth-recovery on 401.

        Variable + node update endpoints share the exact same shape:
        JSON body, ``{successful, data}`` envelope, single-shot 401
        retry through :meth:`Auth.handle_unauthorized`.
        """
        url = f"{self.base_url}{path}"
        kwargs: dict[str, Any] = {"json": body}
        if not self._authenticated:
            await self._authenticate_once()
        kwargs.update(await self.auth.request_kwargs(self.session, self.base_url))
        async with self.session.post(url, **kwargs) as resp:
            if resp.status == 401:
                if not await self.auth.handle_unauthorized(self.session, self.base_url):
                    raise AuthError(f"auth could not recover from 401 on {url}")
                kwargs.update(await self.auth.request_kwargs(self.session, self.base_url))
                async with self.session.post(url, **kwargs) as resp_retry:
                    if resp_retry.status >= 400:
                        raise HTTPError(resp_retry.status, url)
                    text = await resp_retry.text()
            elif resp.status >= 400:
                raise HTTPError(resp.status, url)
            else:
                text = await resp.text()
        try:
            payload = _loads_json(text)
        except ValueError as exc:
            raise ClientError(f"invalid JSON from {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ClientError(f"unexpected non-dict response from {path}")
        return payload


# --- parsers --------------------------------------------------------------


def _coerce_prec(raw: Any) -> int:
    """Normalise a wire-side ``prec`` value to ``int``.

    Returns ``0`` when the field is missing, blank, or non-numeric so
    the dataclass default holds; the controller occasionally omits the
    attribute entirely on properties without scaling.
    """
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


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
            precision=_coerce_prec(prop.get("prec")),
        )

    # ``flag`` arrives stringified from the controller (e.g. ``"128"``
    # for DEVICE_ROOT); coerce defensively in case a future firmware
    # ships it as an int or omits it entirely.
    raw_flag = item.get("flag", 0)
    try:
        flag_int = int(raw_flag)
    except (TypeError, ValueError):
        flag_int = 0

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
        flag=flag_int,
        properties=properties,
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
                precision=_coerce_prec(prop.get("prec")),
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


def parse_rest_nodes_groups_folders(
    xml: str,
) -> tuple[dict[str, GroupRecord], dict[str, FolderRecord]]:
    """Decode ``/rest/nodes`` XML into separate group + folder registries.

    Node entries (``<node>``) in the legacy XML are ignored — the
    JSON ``/api/nodes`` endpoint is the canonical source for those
    and carries the ``family`` / ``instance`` shape we need for the
    nodedef lookup. Only ``<group>`` and ``<folder>`` elements
    contribute to the returned dicts.

    The special "ISY" group (``flag="12"``) representing the
    controller itself is filtered out — it has the controller's MAC
    as its address and isn't a user-facing scene.
    """
    if not xml:
        return {}, {}
    try:
        root = ET.fromstring(xml)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError as exc:
        raise ClientError(f"failed to parse /rest/nodes XML: {exc}") from exc

    groups: dict[str, GroupRecord] = {}
    for group_el in root.findall("group"):
        flag = group_el.get("flag", "0")
        if flag == "12":
            # Controller-self group — skip.
            continue
        addr = group_el.findtext("address") or ""
        if not addr:
            continue
        members: list[str] = []
        controllers: list[str] = []
        for link in group_el.findall("members/link"):
            text = (link.text or "").strip()
            if not text:
                continue
            members.append(text)
            # ``type="16"`` (0x10) marks a scene controller per the legacy
            # IoX wire format; any other value is a responder.
            if link.get("type") == "16":
                controllers.append(text)
        parent_text = group_el.findtext("parent")
        groups[addr] = GroupRecord(
            address=addr,
            name=group_el.findtext("name") or "",
            nodedef_id=group_el.get("nodeDefId", ""),
            family_id=group_el.findtext("family") or "1",
            instance_id="1",
            parent_address=parent_text or None,
            pnode=group_el.findtext("pnode") or None,
            member_addresses=tuple(members),
            controller_addresses=tuple(controllers),
        )

    folders: dict[str, FolderRecord] = {}
    for folder_el in root.findall("folder"):
        addr = folder_el.findtext("address") or ""
        if not addr:
            continue
        parent_text = folder_el.findtext("parent")
        folders[addr] = FolderRecord(
            address=addr,
            name=folder_el.findtext("name") or "",
            family_id=folder_el.findtext("family") or "13",
            parent_address=parent_text or None,
        )
    return groups, folders


def parse_rest_networking_resources(xml: str) -> dict[str, NetworkResourceRecord]:
    """Decode ``/rest/networking/resources`` XML into a record map.

    Wire shape (from eisy / ISY 6+ legacy endpoint, also produced by
    ISY-994 firmware ≥ 4.x)::

        <NetConfig>
          <NetRule>
            <id>1</id>
            <name>Reboot Router</name>
            <host>192.0.2.1</host>
            <!-- ...other fields the runtime doesn't surface... -->
          </NetRule>
        </NetConfig>

    Empty / missing input (controller without networking module
    enabled) returns ``{}``. Malformed XML raises
    :class:`ClientError` so initial-load callers can decide whether
    to abort or treat as "no resources".
    """
    if not xml:
        return {}
    try:
        root = ET.fromstring(xml)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError as exc:
        raise ClientError(f"failed to parse /rest/networking XML: {exc}") from exc

    resources: dict[str, NetworkResourceRecord] = {}
    for rule_el in root.findall("NetRule"):
        rid = (rule_el.findtext("id") or "").strip()
        if not rid:
            continue
        resources[rid] = NetworkResourceRecord(
            address=rid,
            name=rule_el.findtext("name") or "",
        )
    return resources


def parse_api_variables_type(raw: list[dict[str, Any]], type_id: str) -> dict[str, VariableRecord]:
    """Decode one ``/api/variables/{type}`` ``data`` list into typed records.

    Each wire entry is::

        {"id": "<int>", "val": <int>, "init": <int>, "prec": <int>,
         "name": "<str>", "ts": "<ISO8601>"}

    The wire field for the current value is ``val``; this surfaces it
    as :attr:`VariableRecord.value` so consumers don't have to track
    the wire spelling. Entries without an ``id`` are skipped.

    Args:
        raw: The unwrapped ``data`` list from ``/api/variables/{type}``.
        type_id: ``"1"`` (integer) or ``"2"`` (state). Stamped onto each
            record so callers can route writes back to the right
            ``/api/variables/{type}/{id}`` endpoint without carrying the
            type alongside.

    Returns:
        Map of variable id (string) → :class:`VariableRecord`.
    """
    out: dict[str, VariableRecord] = {}
    for entry in raw:
        vid = entry.get("id")
        if vid is None or vid == "":
            continue
        vid_str = str(vid)
        out[vid_str] = VariableRecord(
            type_id=str(type_id),
            id=vid_str,
            name=str(entry.get("name", "")),
            value=_coerce_int(entry.get("val"), default=0),
            init=_coerce_int(entry.get("init"), default=0),
            precision=_coerce_prec(entry.get("prec")),
            ts=str(entry.get("ts", "")),
        )
    return out


def _coerce_int(raw: Any, *, default: int = 0) -> int:
    """Coerce a wire value to ``int``, falling back to ``default`` on junk."""
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def parse_api_programs(raw: list[dict[str, Any]]) -> dict[str, ProgramRecord]:
    """Decode the ``/api/programs`` ``data`` list into typed records.

    Reconstructs each entry's ``path`` by walking the ``parentId``
    chain — the wire payload is a flat list, but consumers expect
    a slash-joined ancestry to drive the legacy
    ``HA.<platform>/<name>/<status|actions>`` folder convention.
    The synthetic root folder name (``"My Programs"`` on stock
    eisy firmware) is dropped from paths so the leading segment
    is the user's first folder.

    Status comes off the wire as the strings ``"true"`` / ``"false"``
    (legacy XML convention preserved); empty / missing strings are
    treated as ``False``. Empty time strings collapse to ``None``.

    Folders inherit ``status`` from the eisy-side aggregation but
    don't carry ``enabled`` / ``run_at_startup`` / ``running`` /
    timing fields — those stay ``None`` on the record.
    """
    by_id: dict[str, dict[str, Any]] = {str(entry.get("id") or ""): entry for entry in raw if entry.get("id")}

    def _path(entry: dict[str, Any]) -> str:
        parts: list[str] = []
        cursor: dict[str, Any] | None = entry
        while cursor is not None:
            parts.append(str(cursor.get("name") or ""))
            parent_id = cursor.get("parentId")
            cursor = by_id.get(str(parent_id)) if parent_id else None
        # Drop the synthetic root segment (always the last one
        # walked — its parentId is absent or unresolved) so the
        # leading path segment is the user's first folder. The root
        # entry itself collapses to an empty string.
        if parts:
            parts = parts[:-1]
        return "/".join(reversed(parts))

    def _str_or_none(value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    records: dict[str, ProgramRecord] = {}
    for prog_id, entry in by_id.items():
        is_folder = bool(entry.get("folder", False))
        status_raw = entry.get("status", "")
        records[prog_id] = ProgramRecord(
            address=prog_id,
            name=str(entry.get("name") or ""),
            path=_path(entry),
            parent_address=(str(entry.get("parentId")) if entry.get("parentId") else None),
            is_folder=is_folder,
            status=str(status_raw).lower() == "true",
            enabled=entry.get("enabled") if not is_folder else None,
            run_at_startup=entry.get("runAtStartup") if not is_folder else None,
            running=_str_or_none(entry.get("running")) if not is_folder else None,
            last_run_time=_str_or_none(entry.get("lastRunTime")),
            last_finish_time=_str_or_none(entry.get("lastFinishTime")),
            next_scheduled_run_time=_str_or_none(entry.get("nextScheduledRunTime")),
        )
    return records


# --- private helpers ------------------------------------------------------


def _unwrap_data(raw: Any, *, source: str = "endpoint") -> list[dict[str, Any]]:
    """Pull the ``data`` array from a ``{successful, data: [...]}`` envelope.

    The eisy ``/api/*`` JSON endpoints all return that envelope. Raises
    :class:`ClientError` when the envelope reports ``successful: false``
    so server-side errors don't get silently flattened to "endpoint is
    empty". A response that is not a dict, or is a dict without a
    ``successful`` key, is treated as legacy/raw and unwrapped
    permissively.

    Args:
        raw: The decoded JSON body.
        source: Short label included in any raised error to help the
            consumer distinguish ``/api/programs`` from ``/api/triggers``
            etc. when the failure surfaces.
    """
    if not isinstance(raw, dict):
        return []
    if raw.get("successful") is False:
        detail = raw.get("error") or raw.get("message") or raw
        raise ClientError(f"{source} returned successful=false: {detail}")
    data = raw.get("data")
    if isinstance(data, list):
        return data
    return []


def _loads_json(text: str) -> Any:
    """Local alias for json.loads — kept as a thin wrapper so tests can
    monkey-patch one symbol if they need to inject decode failures."""
    return json.loads(text)
