"""Shared command-parameter validation for runtime :class:`Node` and :class:`Group`.

Both Node and Group send commands via
``GET /rest/nodes/{addr}/cmd/{cmd}[/{p1}...]`` and validate parameters
against the editor referenced by each command-parameter slot. The
logic is the same; only the target object changes. Keeping it in one
place prevents drift between the two surfaces.

The helper is private (``_commands``) — consumers call
``Node.send_command`` / ``Group.send_command``, never this directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyisyox.schema.editor import EditorCodecError

if TYPE_CHECKING:
    from collections.abc import Sequence

    from pyisyox.schema.cmd import Command
    from pyisyox.schema.nodedef import NodeDef
    from pyisyox.schema.profile import Profile


class NodeCommandError(Exception):
    """Raised when a command can't be sent — unknown command id, missing
    parameter, validation failure, or no nodedef resolved for this node.

    Defined here (not in ``node.py``) to keep the module dependency
    one-way: ``node.py`` imports from ``_commands.py``, never the
    reverse.
    """


def encode_command_params(
    *,
    nodedef: NodeDef | None,
    profile: Profile,
    family_id: str,
    instance_id: str,
    command_id: str,
    params: Sequence[float | str],
    target_label: str,
) -> tuple[tuple[int, str], ...]:
    """Look up ``command_id`` on ``nodedef`` and encode each parameter.

    Args:
        nodedef: The resolved nodedef for the target. ``None`` when no
            nodedef matched the target's ``(nodedef_id, family,
            instance)`` triple — raises immediately.
        profile: The :class:`Profile` containing editors. Editors are
            scoped to ``family_id`` / ``instance_id``.
        family_id: Family id of the target (for editor scoping).
        instance_id: Instance id of the target (for editor scoping).
        command_id: The IoX command id to look up.
        params: Positional command parameters; encoded against the
            corresponding ``CommandParameter.editor_id``.
        target_label: A short label like ``"node 'X'"`` or
            ``"group 'Y'"`` used in error messages so consumers can
            tell which surface raised.

    Returns:
        A tuple of ``(raw_value, uom)`` pairs — one per supplied
        parameter — where ``uom`` is the unit the parameter's editor
        declares (its first range's UOM). ``uom`` is ``""`` for editors
        that carry no real unit. Callers send each parameter as
        ``/{raw_value}/{uom}`` (dropping the UOM segment when empty).

    Raises:
        NodeCommandError: When the nodedef is missing, the command id
            isn't accepted, the parameter count is wrong, or any
            parameter fails editor validation.
    """
    if nodedef is None:
        raise NodeCommandError(
            f"cannot send command {command_id!r} on {target_label}: "
            f"no nodedef resolved for ({command_id!r}, family={family_id!r}, "
            f"instance={instance_id!r})"
        )
    command = _lookup_accepted(nodedef, command_id, target_label)
    return _encode(command, params, profile, family_id, instance_id)


def _lookup_accepted(nodedef: NodeDef, command_id: str, target_label: str) -> Command:
    for cmd in nodedef.cmds.accepts:
        if cmd.id == command_id:
            return cmd
    accept_ids = sorted(c.id for c in nodedef.cmds.accepts)
    raise NodeCommandError(
        f"command {command_id!r} not accepted by nodedef {nodedef.id!r} "
        f"on {target_label} (accepts: {accept_ids})"
    )


def _encode(
    command: Command,
    params: Sequence[float | str],
    profile: Profile,
    family_id: str,
    instance_id: str,
) -> tuple[tuple[int, str], ...]:
    if len(params) > len(command.parameters):
        raise NodeCommandError(
            f"command {command.id!r} accepts {len(command.parameters)} parameter(s); got {len(params)}"
        )
    encoded: list[tuple[int, str]] = []
    for idx, param_def in enumerate(command.parameters):
        if idx >= len(params):
            if not param_def.optional:
                raise NodeCommandError(
                    f"command {command.id!r} requires parameter {idx} "
                    f"(editor {param_def.editor_id!r}) — none provided"
                )
            break
        editor = profile.find_editor(param_def.editor_id, family_id, instance_id)
        if editor is None:
            raise NodeCommandError(
                f"command {command.id!r}: editor {param_def.editor_id!r} "
                f"not found in family {family_id!r} instance {instance_id!r}"
            )
        try:
            raw_value = editor.encode(params[idx])
        except EditorCodecError as exc:
            raise NodeCommandError(
                f"command {command.id!r} parameter {idx} (editor {param_def.editor_id!r}): {exc}"
            ) from exc
        # First range's UOM is the input/display convention the /cmd
        # surface expects appended (e.g. I_OL → "51"). range_for() with
        # no hint already picks the first range, matching encode() above.
        uom = editor.range_for().uom if editor.ranges else ""
        encoded.append((raw_value, uom))
    return tuple(encoded)
