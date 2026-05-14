"""JSON-first HTTP client for IoX 6+ controllers.

Orchestrates the initial load (``/api/config`` → ``/rest/profiles`` →
parallel fan-out of nodes/status/programs/triggers/variables) and
exposes mutation methods. Auth-mode-agnostic — accepts any
:class:`pyisyox.auth.Auth` and retries once on 401 if recovery succeeds.

Total: ≤ 6 HTTP + 1 WebSocket regardless of node-server count
(``/rest/nodes`` was dropped from the fan-out in #127 — its group /
folder data is fully covered by ``/api/nodes`` JSON).

The remaining legacy XML surfaces are ``/rest/status``,
``/rest/nodes/{addr}/cmd/...`` responses, and ``/rest/subscribe`` event
frames; stdlib ``xml.etree.ElementTree`` covers all three.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import quote
from xml.etree import ElementTree as ET

import aiohttp

from pyisyox.auth import Auth, AuthError
from pyisyox.constants import NodeFlag
from pyisyox.logging import LOG_VERBOSE
from pyisyox.paths import (
    CONFIG_PATH,
    NETWORK_RESOURCE_ITEM_PATH,
    NETWORKING_RESOURCES_PATH,
    NLS_PATH,
    NODE_COMMAND_PATH,
    NODE_DISABLE_PATH,
    NODE_ENABLE_PATH,
    NODE_ITEM_PATH,
    NODES_PATH,
    PROFILES_PATH,
    PROGRAM_COMMAND_PATH,
    PROGRAMS_PATH,
    REST_STATUS_PATH,
    TRIGGERS_PATH,
    VARIABLE_ITEM_PATH,
    VARIABLES_TYPE_PATH,
    ZMATTER_ZWAVE_LOCK_CODE_DELETE_PATH,
    ZMATTER_ZWAVE_LOCK_CODE_SET_PATH,
    ZMATTER_ZWAVE_NODEDEFS_PATH,
    ZMATTER_ZWAVE_PARAMETER_GET_PATH,
    ZMATTER_ZWAVE_PARAMETER_SET_PATH,
    ZWAVE_LOCK_CODE_DELETE_PATH,
    ZWAVE_LOCK_CODE_SET_PATH,
    ZWAVE_NODEDEFS_PATH,
    ZWAVE_PARAMETER_GET_PATH,
    ZWAVE_PARAMETER_SET_PATH,
)
from pyisyox.redactor import redact_sensitive
from pyisyox.schema import (
    GLOBAL_NLS_FAMILY_ID,
    Command,
    CommandParameter,
    NLSTable,
    NodeCommands,
    NodeDef,
    NodeLinks,
    NodeProperty,
    Profile,
)

_LOGGER = logging.getLogger(__name__)

#: Method-name → ``aiohttp.ClientSession`` attribute lookup for
#: :meth:`IoXClient._send_json`. Explicit allowlist (rather than
#: ``getattr(session, method.lower())``) so a typo or unsupported verb
#: surfaces as a clear ``ValueError`` instead of silently dispatching
#: to a different session method.
_SEND_JSON_METHODS: dict[str, str] = {
    "POST": "post",
    "PUT": "put",
    "DELETE": "delete",
}


class ClientError(Exception):
    """Base error for client-level failures (HTTP non-2xx, parse errors)."""


class HTTPError(ClientError):
    """Non-2xx response after auth retries are exhausted."""

    def __init__(self, status: int, url: str) -> None:
        """Capture the failing status code and URL."""
        super().__init__(f"HTTP {status} from {url}")
        self.status = status
        self.url = url


class NodeType(StrEnum):
    """Required ``nodeType`` body field on ``POST /api/nodes/{address}``;
    also the lifecycle-event vocabulary. Legacy XML surface uses numeric
    codes — see :class:`pyisyox.constants.UDHierarchyNodeType`."""

    NODE = "node"
    GROUP = "group"
    FOLDER = "folder"


class VariableField(StrEnum):
    """Body keys accepted by ``POST /api/variables/{type}/{id}``;
    one key per request."""

    VALUE = "value"
    INIT = "init"
    NAME = "name"
    PREC = "prec"


@dataclass(slots=True, frozen=True)
class ControllerConfig:
    """Subset of ``/api/config`` that the rest of the load flow needs."""

    uuid: str
    version: str
    portal_host: str | None = None


@dataclass(slots=True)
class NodePropertyValue:
    """One live property value (JSON ``/api/nodes`` or XML ``/rest/status``).

    ``precision``: decimal precision (``raw / 10**precision``). Wire
    field is ``prec``; defaults to ``0`` when omitted.
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
    """One scene/group. Commands to ``address`` broadcast to every member.

    Sourced from ``<group flag="132">`` elements; the special ``flag="12"``
    controller-self group is filtered out at parse time.
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
    """One folder (organisational, no command surface). Family ``"13"``."""

    address: str
    name: str
    family_id: str = "13"
    parent_address: str | None = None


@dataclass(slots=True)
class ProgramRecord:
    """One program or program-folder from ``/api/programs``.

    Programs and folders share the flat list, discriminated by ``is_folder``.
    Status strings ``"true"``/``"false"`` are decoded to bool; empty time
    strings become ``None``. ``path`` is the slash-joined ancestry (excluding
    the ``"My Programs"`` root) to match the pyisy 3.x convention.
    Timestamps stay as ISO 8601 strings so this layer doesn't pull in a
    datetime parser; ``running`` is free-form (``"idle"``,
    ``"running then"``, …).
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
    """One entry from ``/api/variables/{type}``. ``type_id`` is ``"1"``
    (integer) or ``"2"`` (state). Wire field ``val`` is exposed as
    ``value``; ``prec`` is exposed as ``precision``."""

    type_id: str
    id: str
    name: str
    value: int | float = 0
    init: int | float = 0
    precision: int = 0
    ts: str = ""

    @property
    def address(self) -> str:
        """Composite ``{type_id}.{id}`` identifier."""
        return f"{self.type_id}.{self.id}"


@dataclass(slots=True)
class NetworkResourceRecord:
    """One user-defined HTTP/TCP/UDP fire-trigger from
    ``/rest/networking/resources``. ``address`` is the integer id as a
    string for URL-path symmetry."""

    address: str
    name: str


@dataclass(slots=True)
class LoadResult:
    """Output of :meth:`IoXClient.connect`. See attributes for shape."""

    config: ControllerConfig
    profile: Profile
    nodes: dict[str, NodeRecord]
    groups: dict[str, GroupRecord]
    folders: dict[str, FolderRecord]
    programs: dict[str, ProgramRecord]
    triggers: list[dict[str, Any]]
    variables: dict[str, dict[str, VariableRecord]]
    network_resources: dict[str, NetworkResourceRecord]
    root_name: str = ""


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

    async def load(  # pylint: disable=too-many-locals
        self, config: ControllerConfig | None = None
    ) -> LoadResult:
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
            status_xml,
            programs_raw,
            triggers_raw,
            vars_int_raw,
            vars_state_raw,
            networking_xml,
        ) = await asyncio.gather(
            self._get_json(PROFILES_PATH),
            self._get_json(NODES_PATH),
            self._get_text(REST_STATUS_PATH),
            self._get_json(PROGRAMS_PATH),
            self._get_json(TRIGGERS_PATH),
            self._get_json(VARIABLES_TYPE_PATH.format(type_id="1")),
            self._get_json(VARIABLES_TYPE_PATH.format(type_id="2")),
            # Networking module is optional — controllers without it
            # configured return an empty ``<NetConfig/>``. Tolerated by
            # the parser; we don't want a 404 here to abort load, so
            # we fall back to an empty document on HTTP errors.
            self._get_text_or_empty(NETWORKING_RESOURCES_PATH),
        )

        profile = Profile.load_from_json(profile_raw)
        nodes = parse_api_nodes(nodes_raw)
        merge_status_into_nodes(nodes, parse_rest_status(status_xml))
        # /api/nodes JSON carries the full node + group + folder tree
        # (each entry tagged with ``nodeType``) — see issue #127. The
        # legacy ``/rest/nodes`` XML round-trip is dropped from the
        # fan-out; ``parse_rest_nodes_groups_folders`` stays exported
        # for LocalAuth (which doesn't expose ``/api/*``) and external
        # consumers that prefer the XML surface.
        groups, folders, root_name = parse_api_nodes_groups_folders(nodes_raw)
        await self._load_dynamic_zwave_nodedefs(profile, nodes)

        return LoadResult(
            config=config,
            profile=profile,
            nodes=nodes,
            groups=groups,
            folders=folders,
            programs=parse_api_programs(_unwrap_data(programs_raw, source=PROGRAMS_PATH)),
            triggers=_unwrap_data(triggers_raw, source=TRIGGERS_PATH),
            variables={
                "1": parse_api_variables_type(
                    _unwrap_data(vars_int_raw, source=VARIABLES_TYPE_PATH.format(type_id="1")), "1"
                ),
                "2": parse_api_variables_type(
                    _unwrap_data(vars_state_raw, source=VARIABLES_TYPE_PATH.format(type_id="2")), "2"
                ),
            },
            network_resources=parse_rest_networking_resources(networking_xml),
            root_name=root_name,
        )

    #: Family id → ordered ``def/get`` path candidates for radios whose
    #: nodedefs are generated dynamically and therefore absent from
    #: ``/rest/profiles``. ``"4"`` = legacy Z-Wave radio, ``"12"`` =
    #: Z-Matter (800-series). Both candidates are tried because it's not
    #: yet confirmed which family id a Z-Matter setup reports in
    #: ``/api/nodes`` — the controller's own answer is in ``/rest/sys``
    #: ``<SystemOptions><ZMatterZWave>`` (``true`` ⇒ the ``/rest/zmatter/
    #: zwave/...`` surface), but probing both is cheap and avoids the
    #: extra round-trip.
    _DYNAMIC_NODEDEF_PATHS = {
        "4": (ZWAVE_NODEDEFS_PATH, ZMATTER_ZWAVE_NODEDEFS_PATH),
        "12": (ZMATTER_ZWAVE_NODEDEFS_PATH, ZWAVE_NODEDEFS_PATH),
    }

    async def _load_dynamic_zwave_nodedefs(self, profile: Profile, nodes: dict[str, NodeRecord]) -> None:
        """Fetch + merge the dynamic Z-Wave / Z-Matter nodedefs, if needed.

        ``/rest/profiles`` carries the ``ZW_*`` editors but not the
        ``UZW*`` nodedefs, so a Z-Wave node's ``(nodeDefId, family,
        instance)`` lookup comes back empty. For each ``(family,
        instance)`` scope that has at least one such unresolved node, GET
        ``/rest/zwave/node/0/def/get`` (or the Z-Matter variant) once,
        parse the legacy ``<nodeDefs>`` XML, and register the results
        into ``profile`` in place. Best-effort: a 404 (no radio / older
        firmware) or parse error is swallowed — the nodes simply stay
        nodedef-less and ``Node.send_command`` falls back to its
        unvalidated passthrough.
        """
        wanted: set[tuple[str, str]] = set()
        for node in nodes.values():
            if node.family_id not in self._DYNAMIC_NODEDEF_PATHS:
                continue
            if profile.find_nodedef(node.nodedef_id, node.family_id, node.instance_id) is None:
                wanted.add((node.family_id, node.instance_id))
        for family_id, instance_id in wanted:
            for path_tmpl in self._DYNAMIC_NODEDEF_PATHS[family_id]:
                path = path_tmpl.format(address="0")
                try:
                    xml = await self._get_text_or_empty(path)
                    nodedefs = parse_zwave_nodedefs(xml, family_id=family_id, instance_id=instance_id)
                except ClientError as exc:  # pragma: no cover - defensive
                    _LOGGER.debug("Dynamic nodedef load from %s failed: %s", path, exc)
                    continue
                if nodedefs:
                    profile.register_nodedefs(family_id, instance_id, nodedefs)
                    _LOGGER.debug(
                        "Loaded %d dynamic nodedefs for family %s/%s from %s",
                        len(nodedefs),
                        family_id,
                        instance_id,
                        path,
                    )
                    await self._apply_family_nls(profile, family_id, instance_id, nodedefs.values())
                    break

    async def _apply_family_nls(
        self, profile: Profile, family_id: str, instance_id: str, nodedefs: Iterable[NodeDef]
    ) -> None:
        """Fill in NLS labels on dynamically-loaded nodedefs.

        The ``UZW*`` nodedefs parsed from ``def/get`` XML carry no
        command / property / display labels — those live in the per-family
        NLS string tables. Fetch the GLOBAL table (radio-independent
        command + status names) and overlay the radio family's table
        (device-class overrides + enum names) on top, store it on
        ``profile.nls`` (so :meth:`Profile.find_editor` can resolve encoded
        editors' enum names from it), then resolve each nodedef's
        ``Command.name`` (sends + accepts), ``NodeProperty.name``, and
        ``NodeDef.name``. Best-effort: a missing table (404) just leaves
        the relevant labels blank — consumers fall back to the id.
        """
        table = NLSTable()
        for fam in (GLOBAL_NLS_FAMILY_ID, family_id):
            text = await self._get_text_or_empty(NLS_PATH.format(family=fam, instance=instance_id))
            if text.strip():
                table = table.overlay(NLSTable.parse(text))
        if not table.entries:
            return
        profile.nls = profile.nls.overlay(table)
        for nodedef in nodedefs:
            base = nodedef.nls_key
            if not nodedef.name and base:
                resolved = table.nodedef_name(base)
                if resolved:
                    nodedef.name = resolved
            for command in (*nodedef.cmds.sends, *nodedef.cmds.accepts):
                if not command.name:
                    resolved = table.command_name(command.id, base)
                    if resolved:
                        command.name = resolved
            for prop in nodedef.properties.values():
                if not prop.name:
                    resolved = table.property_name(prop.id, base)
                    if resolved:
                        prop.name = resolved

    async def _fetch_config(self) -> ControllerConfig:
        """``GET /api/config`` — confirms IoX 6+ and returns uuid/version."""
        raw = await self._get_json(CONFIG_PATH)
        data = raw.get("data", raw)
        return ControllerConfig(
            uuid=str(data.get("uuid", "")),
            version=str(data.get("version", "")),
            portal_host=data.get("portalHost"),
        )

    async def _authenticate_once(self) -> None:
        """Run ``auth.authenticate`` exactly once across concurrent callers."""
        if self._authenticated:
            return
        if self._auth_lock is None:
            self._auth_lock = asyncio.Lock()
        async with self._auth_lock:
            # Re-read via a local so mypy doesn't narrow it away as unreachable.
            already_authenticated: bool = self._authenticated
            if already_authenticated:
                return
            await self.auth.authenticate(self.session, self.base_url)
            self._authenticated = True

    async def _get_json(self, path: str, *, authenticated: bool = True) -> Any:
        """GET a JSON endpoint. Applies auth and retries once on 401."""
        text = await self._get_text(path, authenticated=authenticated)
        try:
            payload = _loads_json(text)
        except ValueError as exc:
            raise ClientError(f"invalid JSON from {path}: {exc}") from exc
        # _get_text already logged the GET summary at DEBUG. JSON bodies
        # stay at VERBOSE because the profiles blob is ~117 KB.
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "GET %s body: %s", path, redact_sensitive(payload))
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
                text = await resp.text()
            # Wire-trace summary for every GET (JSON + XML). No VERBOSE
            # body dump: XML payloads can be multi-MB and the redactor is
            # JSON-specific.
            _LOGGER.debug("GET %s -> %d bytes", path, len(text))
            return text

    async def _get_text_or_empty(self, path: str) -> str:
        """``_get_text`` that swallows HTTPError → ``""``. For optional
        endpoints (networking) where a missing module 404s."""
        try:
            return await self._get_text(path)
        except HTTPError as exc:
            _LOGGER.debug("optional endpoint %s unavailable: %s", path, exc)
            return ""

    async def send_node_command(self, address: str, command_id: str, *params: int | str) -> str:
        """Issue ``GET /rest/nodes/{addr}/cmd/{cmd}[/{p1}[/{p2}...]]``.

        Params are stringified and joined as-is — the editor codec runs
        in :meth:`Node.send_command`. ``address`` is URL-quoted.
        """
        encoded_addr = quote(address, safe="")
        path_parts = [NODE_COMMAND_PATH.format(address=encoded_addr, command=command_id)]
        path_parts.extend(str(p) for p in params)
        path = "/".join(path_parts)
        return await self._get_text(path)

    async def get_zwave_parameter(self, address: str, number: int, *, zmatter: bool = False) -> str:
        """Issue ``GET /rest/(zmatter/)?zwave/node/{addr}/config/query/{n}``.

        Body on success: ``<config paramNum="N" size="SZ" value="V"/>``.
        Controller failure surfaces as a ``<RestResponse succeeded="false">``
        envelope (caller must inspect — HTTPError covers transport only).
        ``zmatter=True`` switches to the family-12 path prefix.
        """
        encoded_addr = quote(address, safe="")
        path_tmpl = ZMATTER_ZWAVE_PARAMETER_GET_PATH if zmatter else ZWAVE_PARAMETER_GET_PATH
        path = path_tmpl.format(address=encoded_addr, number=number)
        _LOGGER.debug(
            "Z-Wave get parameter %d on %s (zmatter=%s) -> GET %s",
            number,
            address,
            zmatter,
            path,
        )
        body = await self._get_text(path)
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "GET %s body: %s", path, body)
        return body

    async def set_zwave_parameter(
        self,
        address: str,
        number: int,
        value: int,
        size: int,
        *,
        zmatter: bool = False,
    ) -> str:
        """Issue ``GET /rest/(zmatter/)?zwave/node/{addr}/config/set/{n}/{v}/{sz}``.

        ``size`` (1/2/4 bytes) is carried explicitly; the Insteon-style
        ``CONFIG`` command editor doesn't model byte size, so this path
        takes precedence over ``send_command("CONFIG", ...)`` for Z-Wave.
        """
        encoded_addr = quote(address, safe="")
        path_tmpl = ZMATTER_ZWAVE_PARAMETER_SET_PATH if zmatter else ZWAVE_PARAMETER_SET_PATH
        path = path_tmpl.format(address=encoded_addr, number=number, value=value, size=size)
        _LOGGER.debug(
            "Z-Wave set parameter %d=%d (size=%d) on %s (zmatter=%s) -> GET %s",
            number,
            value,
            size,
            address,
            zmatter,
            path,
        )
        body = await self._get_text(path)
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "GET %s body: %s", path, body)
        return body

    async def set_zwave_lock_code(
        self,
        address: str,
        user_num: int,
        code: int,
        *,
        zmatter: bool = False,
    ) -> str:
        """Issue ``GET /rest/(zmatter/)?zwave/node/{addr}/security/user/{n}/set/code/{c}``.

        Programs one user-code slot. Returns a ``<RestResponse>`` envelope
        — callers should pass it through :meth:`Node.set_zwave_lock_code`'s
        parser, which raises on ``succeeded="false"``.
        """
        encoded_addr = quote(address, safe="")
        path_tmpl = ZMATTER_ZWAVE_LOCK_CODE_SET_PATH if zmatter else ZWAVE_LOCK_CODE_SET_PATH
        path = path_tmpl.format(address=encoded_addr, user_num=user_num, code=code)
        _LOGGER.debug(
            "Z-Wave set lock code user_num=%d on %s (zmatter=%s) -> GET %s",
            user_num,
            address,
            zmatter,
            path,
        )
        body = await self._get_text(path)
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "GET %s body: %s", path, body)
        return body

    async def delete_zwave_lock_code(
        self,
        address: str,
        user_num: int,
        *,
        zmatter: bool = False,
    ) -> str:
        """Issue ``GET /rest/(zmatter/)?zwave/node/{addr}/security/user/{n}/delete``.

        Clears one user-code slot.
        """
        encoded_addr = quote(address, safe="")
        path_tmpl = ZMATTER_ZWAVE_LOCK_CODE_DELETE_PATH if zmatter else ZWAVE_LOCK_CODE_DELETE_PATH
        path = path_tmpl.format(address=encoded_addr, user_num=user_num)
        _LOGGER.debug(
            "Z-Wave delete lock code user_num=%d on %s (zmatter=%s) -> GET %s",
            user_num,
            address,
            zmatter,
            path,
        )
        body = await self._get_text(path)
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "GET %s body: %s", path, body)
        return body

    async def set_node_enabled(self, address: str, enabled: bool) -> str:
        """Issue ``GET /rest/nodes/{addr}/{enable|disable}``.

        A disabled node stays in the table; the controller stops polling
        and commanding it.
        """
        encoded_addr = quote(address, safe="")
        path = (NODE_ENABLE_PATH if enabled else NODE_DISABLE_PATH).format(address=encoded_addr)
        return await self._get_text(path)

    async def post_variable_update(
        self, var_type: str | int, var_id: str | int, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Issue ``POST /api/variables/{type}/{id}`` with the supplied body.

        Four documented body shapes (one key per call; eisy-ui doesn't
        mix them):

        * ``{"value": <int>}`` — set the current value
        * ``{"init": <int>}`` — set the initial/restore value
        * ``{"name": "<str>"}`` — rename
        * ``{"prec": <int>}`` — set decimal precision (fires
          ``_1``/``9`` ``VARIABLE_TABLE_CHANGED`` instead of the
          per-value ``6``/``7`` frames; without an auto-refresh
          listener wired to that event, downstream consumers won't
          notice the precision change until the next ``refresh()``).
        """
        path = VARIABLE_ITEM_PATH.format(type_id=var_type, var_id=var_id)
        _LOGGER.debug(
            "Variable write type=%s id=%s body=%s -> POST %s",
            var_type,
            var_id,
            body,
            path,
        )
        response = await self._post_json(path, body)
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "POST %s response: %s", path, response)
        return response

    async def create_variable(self, var_type: str | int, name: str, *, prec: int = 0) -> dict[str, Any]:
        """Create a new variable on the controller.

        Wire shape: ``PUT /api/variables/{type}`` with body
        ``{"name": "<str>", "prec": <int>}``. The controller assigns
        the ``id`` and echoes the new record back as ``data``.

        Note: the eisy controller accepts ``init`` / ``value`` keys in
        the PUT body and even echoes them in the response, but
        silently drops them at storage time (issue #125 captures
        confirm a freshly created variable is always ``val=0`` /
        ``init=0`` regardless of what was sent). Pass ``prec`` here
        and follow up with :meth:`post_variable_update` for value /
        init.

        ``prec=0`` (the controller default) is omitted from the
        request body — there's no "reset to 0" path, only creation,
        so sending the default would just bloat the wire.
        """
        body: dict[str, Any] = {"name": name}
        if prec:
            body["prec"] = prec
        path = VARIABLES_TYPE_PATH.format(type_id=var_type)
        _LOGGER.debug("Variable create type=%s body=%s -> PUT %s", var_type, body, path)
        response = await self._send_json("PUT", path, body)
        if response is None:
            raise ClientError(f"empty response body from PUT {path}")
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "PUT %s response: %s", path, response)
        return response

    async def delete_variable(self, var_type: str | int, var_id: str | int) -> None:
        """Delete a variable.

        Wire shape: ``DELETE /api/variables/{type}/{id}``. Response is
        ``{"successful": true, "data": null}`` (no record echo); a
        ``_1``/``9`` ``VARIABLE_TABLE_CHANGED`` frame fires alongside
        so an auto-refresh listener can drop the entry from the
        registry.
        """
        path = VARIABLE_ITEM_PATH.format(type_id=var_type, var_id=var_id)
        _LOGGER.debug("Variable delete type=%s id=%s -> DELETE %s", var_type, var_id, path)
        await self._send_json("DELETE", path)

    async def get_variables_type(self, var_type: str | int) -> dict[str, VariableRecord]:
        """Fetch + parse one variable type as ``{id: VariableRecord}``.

        Wire shape: ``GET /api/variables/{type}``. Wrapper over the
        connect-time fan-out so consumers (and ``Controller.refresh_variables``)
        don't have to import the private ``_unwrap_data`` /
        ``parse_api_variables_type`` helpers themselves.
        """
        path = VARIABLES_TYPE_PATH.format(type_id=var_type)
        raw = await self._get_json(path)
        return parse_api_variables_type(_unwrap_data(raw, source=path), str(var_type))

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
        return await self._get_text(PROGRAM_COMMAND_PATH.format(program_id=program_id, command=command))

    async def run_network_resource(self, resource_id: str | int) -> str:
        """Fire a network resource by id.

        Wire shape: ``GET /rest/networking/resources/{id}``. Response
        is a small ``<RestResponse status="200">`` envelope on success.
        The controller acknowledges receipt only — it doesn't return
        the result of the underlying HTTP / TCP / UDP fire.
        """
        path = NETWORK_RESOURCE_ITEM_PATH.format(resource_id=resource_id)
        _LOGGER.debug("Network resource fire id=%s -> GET %s", resource_id, path)
        body = await self._get_text(path)
        if _LOGGER.isEnabledFor(LOG_VERBOSE):
            _LOGGER.log(LOG_VERBOSE, "GET %s body: %s", path, body)
        return body

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
        return await self._post_json(NODE_ITEM_PATH.format(address=encoded), body)

    async def _post_json(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """``POST`` shortcut over :meth:`_send_json`.

        Variable + node update endpoints share the exact same shape:
        JSON body, ``{successful, data}`` envelope, single-shot 401
        retry through :meth:`Auth.handle_unauthorized`.
        """
        response = await self._send_json("POST", path, body)
        if response is None:
            raise ClientError(f"empty response body from POST {path}")
        return response

    async def _send_json(
        self,
        method: Literal["POST", "PUT", "DELETE"],
        path: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Shared mutation path: ``PUT`` / ``POST`` / ``DELETE`` with
        ``{successful, data}``-envelope handling and single-shot 401
        recovery.

        Returns the parsed envelope, or ``None`` when the response
        body is empty (``DELETE`` typically returns 200 + no body).
        """
        try:
            session_attr = _SEND_JSON_METHODS[method]
        except KeyError as exc:
            raise ValueError(
                f"unsupported _send_json method {method!r}; expected one of {sorted(_SEND_JSON_METHODS)}"
            ) from exc
        url = f"{self.base_url}{path}"
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body
        if not self._authenticated:
            await self._authenticate_once()
        kwargs.update(await self.auth.request_kwargs(self.session, self.base_url))
        method_fn = getattr(self.session, session_attr)
        async with method_fn(url, **kwargs) as resp:
            if resp.status == 401:
                if not await self.auth.handle_unauthorized(self.session, self.base_url):
                    raise AuthError(f"auth could not recover from 401 on {url}")
                kwargs.update(await self.auth.request_kwargs(self.session, self.base_url))
                async with method_fn(url, **kwargs) as resp_retry:
                    if resp_retry.status >= 400:
                        raise HTTPError(resp_retry.status, url)
                    text = await resp_retry.text()
            elif resp.status >= 400:
                raise HTTPError(resp.status, url)
            else:
                text = await resp.text()
        if not text.strip():
            return None
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


def _flag_int(raw: Any) -> int:
    """Coerce a wire-side ``flag`` value to ``int`` (a :class:`NodeFlag`
    bitfield). The controller stringifies it (e.g. ``"128"``); returns
    ``0`` when it's missing or non-numeric so bit tests are well-defined.
    """
    if raw is None or raw == "":
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def parse_api_nodes_groups_folders(
    raw: dict[str, Any],
) -> tuple[dict[str, GroupRecord], dict[str, FolderRecord], str]:
    """Decode ``/api/nodes`` JSON into group + folder registries + root name.

    The JSON payload nests three parallel arrays under ``data.nodes`` —
    ``node``, ``group``, ``folder`` — each entry tagged with a
    ``nodeType`` discriminator. This walks the ``group`` and ``folder``
    arrays only; nodes are handled by :func:`parse_api_nodes`.

    Drop-in replacement for :func:`parse_rest_nodes_groups_folders`
    (the legacy ``/rest/nodes`` XML parser) — same return shape, same
    ``NodeFlag.ROOT`` filtering, same controller-vs-responder
    ``type="16"`` discrimination on group members. Captured live on
    eisy IoX 6+ confirmed the JSON uses the identical encoding the
    XML did.

    The root group (``flag`` bit ``NodeFlag.ROOT`` set — the
    controller-self pseudo-group whose address is the controller MAC)
    is filtered out of the returned ``groups`` map; its ``name`` is
    surfaced as the third return value so consumers can use the
    user-assigned controller label (e.g. ``"Main eisy"``) for device
    naming — same source the legacy
    ``/rest/config/<configuration><root><name>`` path carried in
    PyISY 3.x. Returns an empty string when the root group is absent
    or unnamed.
    """
    nodes_data = (raw.get("data") or {}).get("nodes") or {}

    groups: dict[str, GroupRecord] = {}
    root_name = ""
    for item in nodes_data.get("group") or []:
        if _flag_int(item.get("flag")) & NodeFlag.ROOT:
            root_name = str(item.get("name") or "") or root_name
            continue
        addr = str(item.get("address") or "")
        if not addr:
            continue
        member_records = (item.get("members") or {}).get("link") or []
        members: list[str] = []
        controllers: list[str] = []
        for link in member_records:
            link_addr = str(link.get("_") or "").strip()
            if not link_addr:
                continue
            members.append(link_addr)
            # ``type="16"`` (0x10) marks a scene controller in both the
            # legacy XML and the JSON. Anything else is a responder.
            if str(link.get("type") or "") == "16":
                controllers.append(link_addr)
        parent = item.get("parent")
        parent_address = parent.get("_") if isinstance(parent, dict) else parent
        pnode = item.get("pnode")
        groups[addr] = GroupRecord(
            address=addr,
            name=str(item.get("name") or ""),
            nodedef_id=str(item.get("nodeDefId") or ""),
            family_id=str(item.get("family") or "1"),
            instance_id="1",
            parent_address=str(parent_address) if parent_address else None,
            pnode=str(pnode) if pnode else None,
            member_addresses=tuple(members),
            controller_addresses=tuple(controllers),
        )

    folders: dict[str, FolderRecord] = {}
    for item in nodes_data.get("folder") or []:
        addr = str(item.get("address") or "")
        if not addr:
            continue
        parent = item.get("parent")
        parent_address = parent.get("_") if isinstance(parent, dict) else parent
        folders[addr] = FolderRecord(
            address=addr,
            name=str(item.get("name") or ""),
            family_id=str(item.get("family") or "13"),
            parent_address=str(parent_address) if parent_address else None,
        )

    return groups, folders, root_name


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

    ``family`` on ``/api/nodes`` JSON arrives in three shapes:

    * **absent** — native Insteon nodes omit it; family / instance default
      to ``"1"``.
    * **bare scalar** — built-in non-Insteon families give a plain string
      (or int), e.g. ``"4"`` for Z-Wave or ``"12"`` for Z-Matter-Z-Wave.
      Built-in families have a single profile instance keyed ``"1"`` (the
      profile carries ``family 4 / instance 1``, not ``4 / 4``), so the
      instance is ``"1"`` — only PG3 plugin families carry a distinct
      instance (their slot id).
    * **mapping** — PG3 plugin nodes give ``{"_": "<id>", "instance":
      "<slot>"}`` (the instance is the plugin slot, distinct from the id).
    """
    family = item.get("family")
    if isinstance(family, dict):
        family_id = str(family.get("_", "1"))
        instance_id = str(family.get("instance", family_id))
    elif family in (None, ""):
        family_id = "1"
        instance_id = "1"
    else:
        family_id = str(family)
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

    flag_int = _flag_int(item.get("flag"))

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
) -> tuple[dict[str, GroupRecord], dict[str, FolderRecord], str]:
    """Decode ``/rest/nodes`` XML into group + folder registries + root name.

    Node entries (``<node>``) in the legacy XML are ignored — the
    JSON ``/api/nodes`` endpoint is the canonical source for those
    and carries the ``family`` / ``instance`` shape we need for the
    nodedef lookup. Only ``<group>`` and ``<folder>`` elements
    contribute to the returned dicts.

    The ``flag`` attribute on ``<group>`` / ``<folder>`` is the same
    :class:`pyisyox.constants.NodeFlag` bitfield used elsewhere (the
    eisy stringifies it — ``"12"`` is ``IS_A_GROUP | ROOT``). The one
    group with :attr:`~pyisyox.constants.NodeFlag.ROOT` set is the
    controller-self pseudo-group (its address is the controller MAC,
    not a user-facing scene) — it's filtered out of the returned
    ``groups`` map, but its ``<name>`` is surfaced as the third return
    value so consumers can use the user-assigned controller name
    (e.g. "Main eisy") for device naming. Returns an empty string
    when the root group is absent or unnamed.
    """
    if not xml:
        return {}, {}, ""
    try:
        root = ET.fromstring(xml)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError as exc:
        raise ClientError(f"failed to parse /rest/nodes XML: {exc}") from exc

    groups: dict[str, GroupRecord] = {}
    root_name = ""
    for group_el in root.findall("group"):
        if _flag_int(group_el.get("flag")) & NodeFlag.ROOT:
            # The controller's own root group — not a user-facing scene.
            # Capture the user-assigned name on the way past.
            root_name = group_el.findtext("name") or root_name
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
    return groups, folders, root_name


def _zwave_cmd_from_xml(cmd_el: ET.Element) -> Command:
    """Build a :class:`Command` from a ``<cmd>`` element in the legacy
    ``<nodeDefs>`` XML (``<cmd id="DON"><p id="" editor="..." optional="T"/>``).

    ``native="F"`` (the only ``native`` value seen on the Z-Wave nodedefs)
    marks a non-native, higher-layer command; its absence means native.
    """
    params: list[CommandParameter] = []
    for p_el in cmd_el.findall("p"):
        editor_id = p_el.get("editor")
        if not editor_id:
            continue
        params.append(
            CommandParameter(
                editor_id=editor_id,
                param_id=p_el.get("id", ""),
                init=p_el.get("init"),
                optional=p_el.get("optional", "").upper() in ("T", "TRUE", "1"),
            )
        )
    return Command(
        id=cmd_el.get("id", ""),
        name=cmd_el.get("name", ""),
        parameters=params,
        native=cmd_el.get("native", "").upper() not in ("F", "FALSE", "0"),
        format=cmd_el.get("fmt"),
    )


def parse_zwave_nodedefs(xml: str, *, family_id: str, instance_id: str) -> dict[str, NodeDef]:
    """Decode ``/rest/zwave/node/{addr}/def/get`` XML into ``{id: NodeDef}``.

    The dynamically-generated Z-Wave nodedefs aren't carried by
    ``/rest/profiles``; this endpoint serves them in the legacy
    ``<nodeDefs><nodedef id="UZW..." nls="..."><sts><st id="ST"
    editor="..."/></sts><cmds><sends/><accepts><cmd .../></accepts></cmds>
    <links><ctl/><rsp><link linkdef="..."/></rsp></links></nodedef></nodeDefs>``
    shape. The ``family_id`` / ``instance_id`` are stamped onto each
    :class:`NodeDef` so it joins against the node's
    ``(nodeDefId, family, instance)`` key. Many referenced editors are
    *encoded ids* (``_51_0_R_0_101_N_IX_DIM_REP``) decoded on demand by
    :meth:`pyisyox.schema.editor.Editor.from_encoded_id`; the named ones
    (``ZW_DIM_PERCENT``, …) are already in ``/rest/profiles`` under
    family ``4``.

    Empty / missing input (no Z-Wave radio) returns ``{}``. Malformed
    XML raises :class:`ClientError`.
    """
    if not xml or not xml.strip():
        return {}
    try:
        root = ET.fromstring(xml)  # noqa: S314 — eisy LAN traffic
    except ET.ParseError as exc:
        raise ClientError(f"failed to parse Z-Wave nodedefs XML: {exc}") from exc

    out: dict[str, NodeDef] = {}
    for nd_el in root.findall("nodedef"):
        nd_id = nd_el.get("id")
        if not nd_id:
            continue
        properties: dict[str, NodeProperty] = {}
        for st_el in nd_el.findall("sts/st"):
            pid = st_el.get("id")
            if not pid:
                continue
            properties[pid] = NodeProperty(
                id=pid,
                editor_id=st_el.get("editor", ""),
                name=st_el.get("name", ""),
                hide=st_el.get("hide", "").upper() in ("T", "TRUE", "1"),
            )
        cmds = NodeCommands(
            sends=[_zwave_cmd_from_xml(c) for c in nd_el.findall("cmds/sends/cmd")],
            accepts=[_zwave_cmd_from_xml(c) for c in nd_el.findall("cmds/accepts/cmd")],
        )
        links = NodeLinks(
            ctl=[ln.get("linkdef", "") for ln in nd_el.findall("links/ctl/link")],
            rsp=[ln.get("linkdef", "") for ln in nd_el.findall("links/rsp/link")],
        )
        out[nd_id] = NodeDef(
            id=nd_id,
            family_id=family_id,
            instance_id=instance_id,
            properties=properties,
            cmds=cmds,
            nls_key=nd_el.get("nls"),
            links=links,
        )
    return out


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
            value=_coerce_var_number(entry.get("val"), default=0),
            init=_coerce_var_number(entry.get("init"), default=0),
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


def _coerce_var_number(raw: Any, *, default: int = 0) -> int | float:
    """Coerce a variable wire value to ``int | float``.

    Variables can store floats on the modern controller (``POST
    /api/variables/{type}/{id}`` accepts both ints and floats), so the
    parser preserves whichever the wire emits — ``int`` for raw
    integer storage, ``float`` for a fresh write that posted a
    fractional value. Bool slips past ``isinstance(bool, int)`` but
    isn't a meaningful variable value here, so it's coerced too.
    """
    if raw is None or raw == "":
        return default
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, (int, float)):
        return raw
    try:
        return int(raw)
    except (TypeError, ValueError):
        try:
            return float(raw)
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
    """Local alias for ``json.loads`` — monkey-patchable from tests."""
    return json.loads(text)
