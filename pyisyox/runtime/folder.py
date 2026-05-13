"""Runtime ``Folder`` — organisational grouping in the controller's node tree.

Folders are pure organisation: they don't accept commands, don't carry
property values, and exist only to group nodes/groups/sub-folders in
the controller UI. The runtime wrapper exposes their identity
(address + name + parent) so consumers can render the tree, but
there's no command surface beyond that.

Folders are sourced from the legacy ``/rest/nodes`` XML response
(``<folder>`` elements). The modern ``/api/nodes`` JSON endpoint
returns nodes only.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyisyox.client import FolderRecord


class Folder:
    """User-facing handle for one folder in the node tree."""

    __slots__ = ("_record",)

    def __init__(self, record: FolderRecord) -> None:
        """Wrap a parsed :class:`FolderRecord`."""
        self._record = record

    @property
    def address(self) -> str:
        """Folder id — typically a 5-digit integer string."""
        return self._record.address

    @property
    def name(self) -> str:
        """User-assigned label."""
        return self._record.name

    @property
    def parent_address(self) -> str | None:
        """Address of the parent folder, or ``None`` for top-level folders."""
        return self._record.parent_address

    @property
    def family_id(self) -> str:
        """Family id — folders use family ``"13"`` (folder family) on IoX."""
        return self._record.family_id

    def to_dict(self) -> dict[str, Any]:
        """Flatten this folder to a JSON-compatible dict.

        Mirrors the underlying :class:`FolderRecord`'s fields; useful
        for the dumper / diagnostics consumers that want a uniform
        snapshot across the controller's collections.
        """
        return asdict(self._record)

    def __repr__(self) -> str:
        parent = f" parent={self.parent_address!r}" if self.parent_address else ""
        return f"Folder(address={self.address!r}, name={self.name!r}{parent})"
