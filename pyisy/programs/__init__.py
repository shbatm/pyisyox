"""Init for management of ISY Programs."""
from __future__ import annotations

from collections.abc import Iterable
import json
from typing import TYPE_CHECKING, Any, cast

from dateutil import parser

from pyisy.constants import URL_PROGRAMS, URL_SUBFOLDERS, XML_TRUE
from pyisy.events.router import EventData
from pyisy.helpers.entity import Entity
from pyisy.helpers.entity_platform import EntityPlatform
from pyisy.logging import _LOGGER, LOG_VERBOSE
from pyisy.programs.folder import Folder, FolderDetail
from pyisy.programs.program import Program, ProgramDetail
from pyisy.helpers.events import EventEmitter

if TYPE_CHECKING:
    from pyisy.isy import ISY

PLATFORM = "programs"
TRUE = "true"

RUN_STATUS: dict[str, str] = {
    "1": "idle",
    "2": "running_then",
    "3": "running_else",
}

PROG_STATUS: dict[str, str | bool] = {
    "1": "unknown",
    "2": True,
    "3": False,
    "F": "not_loaded",
}


class Programs(EntityPlatform):
    """This class handles the ISY programs."""

    def __init__(
        self,
        isy: ISY,
    ) -> None:
        """Initialize the Programs ISY programs manager class.

        Iterate over self.values()
        """
        super().__init__(isy=isy, platform_name=PLATFORM)
        self.status_events = EventEmitter()
        self.url = self.isy.conn.compile_url([URL_PROGRAMS], {URL_SUBFOLDERS: XML_TRUE})

    async def parse(self, xml_dict: dict[str, Any]) -> None:
        """Parse the results from the ISY."""
        if not (features := xml_dict["programs"]["program"]):
            return

        for feature in features:
            await self.parse_entity(feature)
        _LOGGER.info("Loaded %s", PLATFORM)

    async def parse_entity(self, feature: dict[str, Any]) -> None:
        """Parse a single value and add it to the platform."""
        try:
            address = feature["id"]
            name = feature["name"]
            _LOGGER.log(LOG_VERBOSE, "Parsing %s: %s (%s)", PLATFORM, name, address)

            if feature["folder"]:
                entity = Folder(self, address, name, FolderDetail(**feature))
            else:
                entity = Program(self, address, name, ProgramDetail(**feature))

            await self.add_or_update_entity(address, name, entity)
        except (TypeError, KeyError, ValueError) as exc:
            _LOGGER.exception("Error loading %s: %s", PLATFORM, exc)

    async def get_children(self, address: str) -> set[Entity]:
        """Return the children of the a given address."""
        return {e for e in self.values() if e.detail.parent_id == address}

    async def get_tree(self, address: str | None = None) -> dict:
        """Return a tree representation of the entity platform."""
        if address is None:
            roots = {e for e in self.values() if e.detail.parent_id is None}
        else:
            roots = {self.entities[address]}

        # traversal of the tree from top down
        async def traverse(
            hierarchy: dict[str, dict], entities: Iterable[Entity]
        ) -> dict[str, dict]:
            for i in entities:
                children = await self.get_children(i.address)
                hierarchy[i.name] = {
                    "type": type(i).__name__,
                    "address": i.address,
                    "children": await traverse({}, children),
                }
            return hierarchy

        tree = await traverse({}, roots)
        _LOGGER.debug(json.dumps(tree, indent=4, default=str))
        return tree

    async def update_received(self, event: EventData) -> None:
        """Update programs from EventStream message.

        <eventInfo>
        <id></id>
        <X/> ... X=on if enabled, and off if disabled
        <Y/> ... Y=rr if run at reboot and nr if not run at reboot
        <r> last run time in YYMMDD HH:MM:SS</r>
        <f> last finish time in YYMMDD HH:MM:SS</f>
        <s> status* </s>
        </var>
        </eventInfo>
        """
        event_info = cast(dict, event.event_info)
        if (address := cast(str, event_info["id"]).zfill(4)) not in self.addresses:
            # New/unknown program, refresh full set.
            await self.update()
            return
        entity = self.entities[address]
        detail = cast(ProgramDetail, entity.detail)

        if "on" in event_info:
            detail.enabled = True
        elif "off" in event_info:
            detail.enabled = False

        if "rr" in event_info:
            detail.run_at_startup = True
        elif "nr" in event_info:
            detail.run_at_startup = False

        if status := event_info.get("s"):
            # Status is a bitwise OR of RUN_X and ST_X:
            # RUN_IDLE = 0x01
            # RUN_THEN = 0x02
            # RUN_ELSE = 0x03
            # ST_UNKNOWN = 0x10
            # ST_TRUE = 0x20
            # ST_FALSE = 0x30
            # ST_NOT_LOADED = 0xF0
            detail.status = PROG_STATUS[status[0]]
            detail.running = RUN_STATUS[status[1]]

        if last_run := event_info.get("r"):
            detail.last_run_time = parser.parse(last_run)

        if last_finish := event_info.get("f"):
            detail.last_finish_time = parser.parse(last_finish)

        entity.update_status(entity.status, force=True)

        _LOGGER.debug(
            "Updated program: address=%s, detail=%s",
            address,
            json.dumps(detail.__dict__, default=str),
        )
