"""Command and command-parameter dataclasses for IoX nodedefs.

A ``Command`` is an action a node can either *send* (emit as an event) or
*accept* (receive as an instruction). ``CommandParameter`` describes one
positional argument the command takes; the parameter's ``editor`` reference
resolves to an :class:`~pyisyox.schema.editor.Editor` and provides
write-side validation.

Source schema: ``/rest/profiles?include=nodedefs`` ``cmds.{sends,accepts}[]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CommandParameter:
    """A single positional parameter on a command.

    Attributes:
        editor_id: Reference to the editor defining valid values for this
            parameter. Resolves against the parent profile's editor table.
        param_id: Optional parameter identifier (often empty in IoX).
        init: Optional property whose current value seeds this parameter
            (e.g., ``"CLISPH"`` — the heat setpoint command's parameter
            initialises from the current ``CLISPH`` property).
        optional: Whether the parameter may be omitted on send.
    """

    editor_id: str
    param_id: str = ""
    init: str | None = None
    optional: bool = False


@dataclass(slots=True)
class Command:
    """A command a node sends or accepts.

    Attributes:
        id: Command identifier (e.g., ``"DON"``, ``"CLISPC"``, ``"DISCOVER"``).
        name: Human-readable label.
        parameters: Positional parameters; empty for parameterless commands.
        native: Whether the command is a native IoX command (``"true"``) or
            implemented at a higher layer.
        format: Optional display format string used by the controller's UI.
    """

    id: str
    name: str = ""
    parameters: list[CommandParameter] = field(default_factory=list)
    native: bool = False
    format: str | None = None

    @classmethod
    def from_json(cls, raw: dict) -> Command:
        """Build a :class:`Command` from a JSON object as found in
        ``/rest/profiles`` nodedef ``cmds.sends[]`` / ``cmds.accepts[]``.

        Defensive against partial / null fields under PG3 dynamic
        profile reload — a parameter without an ``editor`` key is
        skipped rather than raising ``KeyError`` on the whole nodedef.
        """
        params: list[CommandParameter] = []
        for p in raw.get("parameters") or []:
            if not isinstance(p, dict):
                continue
            editor_id = p.get("editor")
            if not editor_id:
                continue
            params.append(
                CommandParameter(
                    editor_id=editor_id,
                    param_id=p.get("id", ""),
                    init=p.get("init"),
                    optional=bool(p.get("optional", False)),
                )
            )
        return cls(
            id=raw["id"],
            name=raw.get("name", ""),
            parameters=params,
            native=str(raw.get("native", "false")).lower() == "true",
            format=raw.get("format"),
        )
