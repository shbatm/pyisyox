"""Runtime objects that wrap :class:`pyisyox.client.LoadResult` data.

Phase 4a deliverable: :class:`Node` (wraps :class:`NodeRecord` plus the
resolved :class:`NodeDef` plus a back-reference to the
:class:`IoXClient` for command sends), with an editor-codec-validated
:meth:`Node.send_command`.

Group/Folder/Program/Variable runtime classes follow in subsequent
phase 4 commits; this module deliberately stays narrow so the new code
path is testable in isolation before it replaces the legacy
:mod:`pyisyox.nodes` / :mod:`pyisyox.programs` modules.
"""

from pyisyox.runtime.node import Node, NodeCommandError

__all__ = ["Node", "NodeCommandError"]
