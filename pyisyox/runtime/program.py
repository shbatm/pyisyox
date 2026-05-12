"""Runtime ``Program`` and ``ProgramFolder`` wrappers.

Programs and program-folders share the controller's flat program
list and the same ``/rest/programs/{id}/...`` command surface, but
folders only support a subset of commands (typically ``run`` /
``stop`` / ``enable`` / ``disable``). The runtime layer keeps them
as separate types so consumers can branch on isinstance instead of
a runtime ``is_folder`` flag.

State updates flow over the WebSocket: a ``<control>_1</control>``
frame with ``<action>0</action>`` carries an ``<eventInfo>`` body
that updates the program's status, last-run / last-finish times,
and running state. The :class:`pyisyox.runtime.EventDispatcher`
owns the parse + apply path; this module just exposes the data
shape the dispatcher mutates.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyisyox.client import IoXClient, ProgramRecord


class ProgramCommand(StrEnum):
    """Verbs accepted by ``GET /rest/programs/{id}/{command}``.

    Members are the camelCase wire strings the eisy expects;
    consumers building HA-style snake-case service schemas can use
    the member names (``ProgramCommand.RUN_THEN.name == "RUN_THEN"``)
    or pull the wire string via ``.value`` / direct comparison
    (``StrEnum`` members compare equal to their underlying string).

    Folders only support :attr:`RUN`, :attr:`STOP`, :attr:`ENABLE`,
    and :attr:`DISABLE` — :class:`Program`-only verbs raise
    server-side on a folder target.
    """

    #: Run the program (or every program under a folder). For
    #: programs, evaluates the if-clause and runs the matching branch.
    RUN = "run"
    #: Run the program's ``then`` clause directly.
    RUN_THEN = "runThen"
    #: Run the program's ``else`` clause directly.
    RUN_ELSE = "runElse"
    #: Re-evaluate the program's ``if`` condition without running
    #: the matching clause's actions.
    RUN_IF = "runIf"
    #: Abort an executing program / folder.
    STOP = "stop"
    #: Enable the program / folder for evaluation.
    ENABLE = "enable"
    #: Disable the program / folder (status freezes).
    DISABLE = "disable"
    #: Mark the program as auto-run on controller boot.
    ENABLE_RUN_AT_STARTUP = "enableRunAtStartup"
    #: Clear the auto-run-on-boot flag.
    DISABLE_RUN_AT_STARTUP = "disableRunAtStartup"


class _ProgramBase:
    """Shared identity surface for :class:`Program` and :class:`ProgramFolder`."""

    __slots__ = ("_client", "_record")

    def __init__(self, record: ProgramRecord, client: IoXClient) -> None:
        self._record = record
        self._client = client

    @property
    def address(self) -> str:
        """Program / folder id (4-character hex string)."""
        return self._record.address

    @property
    def name(self) -> str:
        """User-assigned label."""
        return self._record.name

    @property
    def path(self) -> str:
        """Slash-joined ancestry, excluding the synthetic root.

        Consumers driving the legacy ``HA.<platform>/<name>/<status|actions>``
        folder convention read this directly; the leading segment is
        the user's first folder rather than the controller's
        ``"My Programs"`` container.
        """
        return self._record.path

    @property
    def parent_address(self) -> str | None:
        """Parent folder id, or ``None`` for the root."""
        return self._record.parent_address

    @property
    def status(self) -> bool:
        """Result of the program's last evaluation. For folders, the
        eisy-side aggregation across children."""
        return self._record.status

    async def run(self) -> None:
        """Run the program (or every program under a folder).

        Wire: ``GET /rest/programs/{id}/run``.
        """
        await self._client.run_program_command(self._record.address, ProgramCommand.RUN)

    async def stop(self) -> None:
        """Stop a running program / folder."""
        await self._client.run_program_command(self._record.address, ProgramCommand.STOP)

    async def enable(self) -> None:
        """Enable the program / folder."""
        await self._client.run_program_command(self._record.address, ProgramCommand.ENABLE)

    async def disable(self) -> None:
        """Disable the program / folder.

        Disabled programs are not evaluated (status freezes); folders
        block evaluation of every program inside them.
        """
        await self._client.run_program_command(self._record.address, ProgramCommand.DISABLE)


class Program(_ProgramBase):
    """User-facing handle for one program."""

    @property
    def enabled(self) -> bool | None:
        """``False`` when the program is disabled. ``None`` if the
        wire payload omitted the field (defensive — every captured
        program carries it)."""
        return self._record.enabled

    @property
    def run_at_startup(self) -> bool | None:
        """``True`` if the program is set to run on controller boot."""
        return self._record.run_at_startup

    @property
    def running(self) -> str | None:
        """Free-form runtime state: ``"idle"`` for idle programs;
        running programs report ``"running then"`` / ``"running else"``
        / ``"running if"`` etc."""
        return self._record.running

    @property
    def last_run_time(self) -> str | None:
        """ISO 8601 timestamp string (``"2026-05-10T14:49:53.000Z"``)
        or ``None`` if the program has never run."""
        return self._record.last_run_time

    @property
    def last_finish_time(self) -> str | None:
        """ISO 8601 timestamp string or ``None``."""
        return self._record.last_finish_time

    @property
    def next_scheduled_run_time(self) -> str | None:
        """ISO 8601 timestamp string or ``None`` if there's no
        scheduled run (manual-only programs)."""
        return self._record.next_scheduled_run_time

    async def run_then(self) -> None:
        """Run the program's ``then`` clause.

        Wire: ``GET /rest/programs/{id}/runThen``.
        """
        await self._client.run_program_command(self._record.address, ProgramCommand.RUN_THEN)

    async def run_else(self) -> None:
        """Run the program's ``else`` clause."""
        await self._client.run_program_command(self._record.address, ProgramCommand.RUN_ELSE)

    async def run_if(self) -> None:
        """Re-evaluate the program's ``if`` condition (without running
        the matching clause's actions)."""
        await self._client.run_program_command(self._record.address, ProgramCommand.RUN_IF)

    async def enable_run_at_startup(self) -> None:
        """Mark the program as auto-run on controller boot."""
        await self._client.run_program_command(self._record.address, ProgramCommand.ENABLE_RUN_AT_STARTUP)

    async def disable_run_at_startup(self) -> None:
        """Clear the auto-run-on-boot flag."""
        await self._client.run_program_command(self._record.address, ProgramCommand.DISABLE_RUN_AT_STARTUP)

    def __repr__(self) -> str:
        return (
            f"Program(address={self.address!r}, name={self.name!r}, path={self.path!r}, status={self.status})"
        )


class ProgramFolder(_ProgramBase):
    """Organisational container for programs.

    Folders share the program command surface but only ``run`` /
    ``stop`` / ``enable`` / ``disable`` are documented to apply.
    The eisy aggregates child status into ``status`` server-side.
    """

    def __repr__(self) -> str:
        return f"ProgramFolder(address={self.address!r}, name={self.name!r}, path={self.path!r})"
