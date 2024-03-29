"""ISY Node Server Information."""
from __future__ import annotations

import asyncio
from dataclasses import InitVar, asdict, dataclass, field
import inspect
import json
import re
from typing import TYPE_CHECKING, Any

from pyisyox.constants import ATTR_ID, DEFAULT_DIR, TAG_NAME, UOM_INDEX, URL_PROFILE_NS
from pyisyox.helpers.xml import parse_xml
from pyisyox.logging import _LOGGER
from pyisyox.util.output import write_to_file

if TYPE_CHECKING:
    from pyisyox.isy import ISY


ATTR_ACCEPTS = "accepts"
ATTR_CMD = "cmd"
ATTR_CONNECTION = "connection"
ATTR_CONNECTIONS = "connections"
ATTR_DIR = "dir"
ATTR_EDITOR = "editor"
ATTR_EDITORS = "editors"
ATTR_FILE = "file"
ATTR_FILES = "files"
ATTR_NLS = "nls"
ATTR_NODE_DEF = "node_def"
ATTR_NODE_DEFS = "node_defs"
ATTR_NODEDEF = "nodedef"
ATTR_PROFILE = "profile"
ATTR_PROFILES = "profiles"
ATTR_RANGE = "range"
ATTR_SENDS = "sends"
ATTR_ST = "st"
ATTR_SUBSET = "subset"

LANG_EN_US = "en_us"

URL_NS_ALL = "0"

# Regex to remove comments and blank lines from NLS files
NLS_CLEAN = re.compile(r"^(#.*)?\r?\n", flags=re.M)
NLS_SPLIT = re.compile(r"\s+=\s+")


@dataclass
class EditorRange:
    """Node Server Editor Range definition."""

    @classmethod
    def from_dict(cls, props: dict) -> EditorRange:
        """Create a dataclass from a dictionary."""
        return cls(
            **{k: v for k, v in props.items() if k in inspect.signature(cls).parameters}
        )

    uom: str = ""
    min: str = ""
    max: str = ""
    step: int = 0
    precision: int = 0
    subset: str = ""
    nls: str = ""


@dataclass
class NodeEditor:
    """Node Server Editor definition."""

    @classmethod
    def from_dict(cls, props: dict) -> NodeEditor:
        """Create a dataclass from a dictionary."""
        return cls(
            **{k: v for k, v in props.items() if k in inspect.signature(cls).parameters}
        )

    editor_id: str = ""
    # Ranges are a dict with UoM as the key
    ranges: dict[str, EditorRange] = field(default_factory=dict)
    nls: str = ""
    slot: str = ""
    values: dict[int, str] = field(default_factory=dict)


@dataclass
class NodeServerConnection:
    """Node Server Connection details."""

    @classmethod
    def from_dict(cls, props: dict) -> NodeServerConnection:
        """Create a dataclass from a dictionary."""
        return cls(
            **{k: v for k, v in props.items() if k in inspect.signature(cls).parameters}
        )

    profile: str = ""
    type_: str = ""
    enabled: bool = False
    name: str = ""
    ssl: bool = False
    sni: bool = False
    port: str = ""
    timeout: str = ""
    isyusernum: str = ""
    ip: str = ""
    baseurl: str = ""
    nsuser: str = ""

    def configuration_url(self) -> str:
        """Compile a configuration url from the connection data."""
        protocol: str = "https://" if self.ssl else "http://"
        return f"{protocol}{self.ip}:{self.port}"


@dataclass
class NodeDef:
    """Node Server Node Definition parsed from the ISY/IoX."""

    @classmethod
    def from_dict(cls, props: dict) -> NodeDef:
        """Create a dataclass from a dictionary."""
        return cls(
            **{k: v for k, v in props.items() if k in inspect.signature(cls).parameters}
        )

    sts: InitVar[dict[str, list | dict]]
    cmds: InitVar[dict[str, Any]] | None = None
    id: str = ""
    node_type: str = ""
    name: str = ""
    nls: str = ""
    slot: str = ""
    editors: Any = ""
    statuses: dict[str, str] = field(init=False, default_factory=dict)
    status_names: dict[str, str] = field(default_factory=dict)
    status_editors: dict[str, NodeEditor] = field(default_factory=dict)
    sends: dict[str, Any] = field(init=False, default_factory=dict)
    accepts: dict[str, Any] = field(init=False, default_factory=dict)

    def __post_init__(
        self, sts: dict[str, list | dict], cmds: dict[str, Any] | None
    ) -> None:
        """Post-process node server definition."""
        statuses = {}
        if sts:
            if isinstance(st_list := sts[ATTR_ST], dict):
                st_list = [st_list]
            for st in st_list:
                statuses.update({st[ATTR_ID]: st[ATTR_EDITOR]})
        self.statuses = statuses

        if cmds is None:
            return
        if cmds_sends := cmds.get(ATTR_SENDS):
            if isinstance((cmd_list := cmds_sends.get(ATTR_CMD)), dict):
                cmd_list = [cmd_list]
            self.sends = {i[ATTR_ID]: i for i in cmd_list}

        if cmds_accepts := cmds.get(ATTR_ACCEPTS):
            if isinstance((cmd_list := cmds_accepts.get(ATTR_CMD)), dict):
                cmd_list = [cmd_list]
            self.accepts = {i[ATTR_ID]: i for i in cmd_list}


class NodeServers:
    """ISY NodeServers class object.

    DESCRIPTION:
        This class handles the ISY Node Servers info.

    Attributes:
        isy: The ISY device class

    """

    isy: ISY
    _connections: list[NodeServerConnection]
    slots: set[str] = set()
    _node_server_node_definitions: dict[str, dict[str, NodeDef]] = {}
    _node_server_node_editors: dict[str, dict[str, NodeEditor]] = {}
    _node_server_nls: dict = {}
    loaded: bool
    bg_tasks: set = set()

    def __init__(self, isy: ISY):
        """Initialize the NodeServers class."""
        self.isy = isy
        self.loaded = False
        self._connections = []

    async def update(self) -> None:
        """Load information about node servers from the ISY."""
        ns_conn_task = asyncio.create_task(self.get_connection_info())
        self.bg_tasks.add(ns_conn_task)
        ns_conn_task.add_done_callback(self.bg_tasks.discard)

        ns_profile_task = asyncio.create_task(self.get_node_server_profiles())
        self.bg_tasks.add(ns_profile_task)
        ns_profile_task.add_done_callback(self.bg_tasks.discard)

        while self.bg_tasks:
            await asyncio.sleep(0.05)

        for slot in self.slots:
            self.parse_nls_info_for_slot(slot)
        self.loaded = True
        _LOGGER.info("Updated node servers")

    async def get_connection_info(self) -> None:
        """Fetch the node server connections from the ISY."""
        result = await self.isy.conn.request(
            self.isy.conn.compile_url([URL_PROFILE_NS, URL_NS_ALL, ATTR_CONNECTION]),
            ok404=False,
        )
        if result is None:
            return

        ns_conn_xml = parse_xml(result)

        if self.isy.args and self.isy.args.file:
            await self.isy.loop.run_in_executor(
                None,
                write_to_file,
                ns_conn_xml,
                f"{DEFAULT_DIR}node-server-connections.yaml",
            )

        if not (connections := ns_conn_xml[ATTR_CONNECTIONS]):
            return

        if isinstance((connection_list := connections[ATTR_CONNECTION]), dict):
            connection_list = [connection_list]  # Handle case for 1 Node Server

        for connection in connection_list:
            self.parse_connection(connection)

        _LOGGER.debug("Updated node server connection info")

    def parse_connection(self, conn: dict) -> None:
        """Parse the node server connection files from the ISY."""
        try:
            self._connections.append(NodeServerConnection.from_dict(conn))
        except (ValueError, KeyError, NameError) as exc:
            _LOGGER.error("Could not parse node server connection: %s", exc)
            return

    async def get_node_server_profiles(self) -> None:
        """Retrieve the node server definition files from the ISY."""
        result = await self.isy.conn.request(
            self.isy.conn.compile_url([URL_PROFILE_NS, URL_NS_ALL, ATTR_FILES]),
            ok404=False,
        )

        if result is None:
            return

        ns_conn_xml = parse_xml(result)

        if self.isy.args and self.isy.args.file:
            await self.isy.loop.run_in_executor(
                None,
                write_to_file,
                ns_conn_xml,
                f"{DEFAULT_DIR}node-server-profiles.yaml",
            )

        if not (profiles := ns_conn_xml[ATTR_PROFILES]):
            return

        if isinstance((profile_list := profiles[ATTR_PROFILE]), dict):
            profile_list = [profile_list]  # Handle case for 1 Node Server

        for profile in profile_list:
            self.parse_profile(profile)

        _LOGGER.debug("Downloaded node server files")

    def parse_profile(self, profile: dict) -> None:
        """Parse the node server profile file list from the ISY."""
        try:
            slot = profile[ATTR_ID]
            files: list[dict] = profile[ATTR_FILES]

            for file in files:
                dir_name = file[ATTR_DIR]
                file_name = file[ATTR_FILE][TAG_NAME]
                file_path = f"{slot}/download/{dir_name}/{file_name}"

                task = asyncio.create_task(self.fetch_node_server_file(file_path))
                self.bg_tasks.add(task)
                task.add_done_callback(self.bg_tasks.discard)

            self.slots.add(slot)

        except (ValueError, KeyError, NameError) as exc:
            _LOGGER.error("Could not parse node server profile: %s", exc)
            return

    async def fetch_node_server_file(self, path: str) -> None:
        """Fetch a node server file from the ISY."""
        result = await self.isy.conn.request(
            self.isy.conn.compile_url([URL_PROFILE_NS, path])
        )
        if result is None:
            return
        await self.parse_node_server_file(path, result)

    async def parse_node_server_file(self, path: str, file_content: str) -> None:
        """Retrieve and parse the node server definitions."""
        slot = path.split("/")[0]
        path = path.lower()

        _LOGGER.debug(
            "Parsing node server %s file %s", slot, "/".join(path.split("/")[-2:])
        )
        if path.endswith(".xml"):
            xml_dict = parse_xml(file_content)

            if self.isy.args and self.isy.args.file:
                filename = "-".join(path.split("/")[-2:]).replace(".xml", ".yaml")
                await self.isy.loop.run_in_executor(
                    None,
                    write_to_file,
                    xml_dict,
                    f"{DEFAULT_DIR}ns-{slot}-{filename}",
                )

            if ATTR_NODEDEF in path:
                if not (node_defs := xml_dict[ATTR_NODE_DEFS]):
                    return
                if isinstance((nd_list := node_defs[ATTR_NODE_DEF]), dict):
                    nd_list = [nd_list]
                self._node_server_node_definitions[slot] = {}
                for node_def in nd_list:
                    self.parse_node_server_defs(slot, node_def)
                return
            if ATTR_EDITOR in path:
                if not (editors := xml_dict[ATTR_EDITORS]):
                    return
                if isinstance((editor_list := editors[ATTR_EDITOR]), dict):
                    editor_list = [editor_list]
                self._node_server_node_editors[slot] = {}
                for editor in editor_list:
                    self.parse_node_server_editor(slot, editor)
                return
        elif f"{ATTR_NLS}/{LANG_EN_US}" in path:
            nls_lookup: dict = {}
            try:
                nls_lookup = dict(
                    NLS_SPLIT.split(line.strip(), maxsplit=1)
                    for line in NLS_CLEAN.sub("", file_content).strip().split("\n")
                )
            except ValueError as err:
                _LOGGER.error(
                    "Error processing language file for node server slot %s, invalid format: %s\n%s",
                    slot,
                    err,
                    repr(file_content),
                )
            self._node_server_nls[slot] = nls_lookup

            if self.isy.args and self.isy.args.file:
                filename = "-".join(path.split("/")[-2:]).replace(".txt", ".yaml")
                await self.isy.loop.run_in_executor(
                    None,
                    write_to_file,
                    nls_lookup,
                    f"{DEFAULT_DIR}ns-{slot}-{filename}",
                )

            return
        _LOGGER.warning(
            "Unknown file for slot %s: %s", slot, "/".join(path.split("/")[-2:])
        )

    def parse_node_server_defs(self, slot: str, node_def: dict) -> None:
        """Retrieve and parse the node server definitions."""
        try:
            self._node_server_node_definitions[slot][
                node_def[ATTR_ID]
            ] = NodeDef.from_dict(node_def)

        except (ValueError, KeyError, NameError) as exc:
            _LOGGER.error("Could not parse node server definition: %s", exc)
            return

    def parse_node_server_editor(self, slot: str, editor: dict) -> None:
        """Retrieve and parse the node server definitions."""
        editor_id = editor[ATTR_ID]
        if isinstance((ranges := editor[ATTR_RANGE]), dict):
            ranges = [ranges]
        editor_ranges = {}
        for rng in ranges:
            editor_ranges[rng["uom"]] = EditorRange.from_dict(rng)

        self._node_server_node_editors[slot][editor_id] = NodeEditor(
            editor_id=editor_id,
            ranges=editor_ranges,
            slot=slot,
        )

    def parse_nls_info_for_slot(self, slot: str) -> None:
        """Fetch the node server connections from the ISY."""
        try:
            # Update NLS information
            if not (nls := self._node_server_nls.get(slot)):
                # Missing NLS file for this node server
                return
            if not (editors := self._node_server_node_editors.get(slot)):
                return

            for editor in editors.values():
                if (index_range := editor.ranges.get(UOM_INDEX)) and index_range.nls:
                    editor.values = {
                        int(k.replace(f"{index_range.nls}-", "")): v
                        for k, v in nls.items()
                        if k.startswith(f"{index_range.nls}-")
                    }

            if not (node_defs := self._node_server_node_definitions.get(slot)):
                return
            for node_def in node_defs.values():
                if (name_key := f"ND-{node_def.id}-NAME") in nls:
                    node_def.name = nls[name_key]

                for st_id, st_editor in node_def.statuses.items():
                    if (key := f"ST-{node_def.nls}-{st_id}-NAME") in nls:
                        node_def.status_names[st_id] = nls[key]
                    node_def.status_editors[st_id] = editors[st_editor]
        except (ValueError, KeyError, NameError) as exc:
            _LOGGER.error(
                "Error parsing language information for node server slot %s: %s",
                slot,
                exc,
            )

    def to_dict(self) -> dict:
        """Dump entity platform entities to dict."""
        return {
            ATTR_CONNECTIONS: [asdict(conn) for conn in self._connections],
            ATTR_NODE_DEFS: {
                slot: {k: asdict(v) for k, v in node_def.items()}
                for slot, node_def in self._node_server_node_definitions.items()
            },
        }

    @property
    def profiles(self) -> dict[str, dict[str, NodeDef]]:
        """Return the compiled node server profiles."""
        return self._node_server_node_definitions

    def __str__(self) -> str:
        """Return a string representation of the node servers."""
        return f"<{type(self).__name__} slots={self.slots} loaded={self.loaded}>"

    def __repr__(self) -> str:
        """Return a string representation of the node servers."""
        return (
            f"<{type(self).__name__} slots={self.slots} loaded={self.loaded}>"
            f" detail:\n{json.dumps(self._node_server_node_definitions, sort_keys=True, default=str)}"
        )
