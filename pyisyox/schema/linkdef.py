"""Link definitions — Insteon scene/link metadata referenced from nodedefs.

A LinkDef describes a parameter slot on an Insteon link record. Most pyisyox
consumers will not interact with LinkDefs directly; they're carried through
so plugin/profile parity is preserved.

Source schema: ``/rest/profiles`` instance ``linkdefs[]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class LinkParameter:
    """A single parameter on a link definition.

    Attributes:
        editor_id: Reference to the editor defining valid values.
        param_id: Parameter identifier within the link record.
        init: Optional initial-value source.
    """

    editor_id: str
    param_id: str = ""
    init: str | None = None


@dataclass(slots=True)
class LinkDef:
    """A link definition, identified by id and carrying parameter slots.

    Attributes:
        id: Link definition identifier (e.g., ``"DON_OL"``, ``"DON_LinkPair"``).
        parameters: Parameter slots in declaration order.
    """

    id: str
    parameters: list[LinkParameter] = field(default_factory=list)

    @classmethod
    def from_json(cls, raw: dict) -> LinkDef:
        """Build a :class:`LinkDef` from a JSON object."""
        params = [
            LinkParameter(
                editor_id=p.get("editor", ""),
                param_id=p.get("id", ""),
                init=p.get("init"),
            )
            for p in raw.get("parameters", [])
        ]
        return cls(id=raw["id"], parameters=params)
