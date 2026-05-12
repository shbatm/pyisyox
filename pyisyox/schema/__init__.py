"""Schema dataclasses for IoX 6 ``/rest/profiles`` JSON.

This package vendors the schema layer of UDI's nucore-ai library: the
type-equivalents for cmd, editor, linkdef, nodedef, and uom, plus a
JSON-input loader for the unified profile blob.

The schema is intentionally separated from the wire layer so that
connection code in :mod:`pyisyox.connection` can be rewritten freely
while the data model remains stable. Editors carry a bidirectional codec
(see :mod:`pyisyox.schema.editor`) covering both controller→display
decoding and command-parameter encoding with subset/range validation.
"""

from pyisyox.schema.cmd import Command, CommandParameter
from pyisyox.schema.editor import Editor, EditorCodecError, EditorRange
from pyisyox.schema.linkdef import LinkDef, LinkParameter
from pyisyox.schema.nodedef import NodeCommands, NodeDef, NodeLinks, NodeProperty, Property
from pyisyox.schema.profile import Family, Instance, Profile
from pyisyox.schema.uom import PREDEFINED_UOMS, UNKNOWN_UOM, UOMEntry, get_uom

__all__ = [
    "PREDEFINED_UOMS",
    "UNKNOWN_UOM",
    "Command",
    "CommandParameter",
    "Editor",
    "EditorCodecError",
    "EditorRange",
    "Family",
    "Instance",
    "LinkDef",
    "LinkParameter",
    "NodeCommands",
    "NodeDef",
    "NodeLinks",
    "NodeProperty",
    "Profile",
    "Property",
    "UOMEntry",
    "get_uom",
]
