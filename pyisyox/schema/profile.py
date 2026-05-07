"""Profile loader for IoX ``/rest/profiles`` JSON responses.

Parses one profile blob (families → instances → editors/linkdefs/nodedefs)
into resolved Python objects, then builds the
``(nodedef_id, family_id, instance_id) → NodeDef`` lookup that lets a node
from ``/api/nodes`` find its definition.

This module knows the JSON wire shape but does no HTTP. Callers pass
already-fetched dicts to :meth:`Profile.load_from_json`.

Source schema: ``/rest/profiles?include=nodedefs,editors,linkdefs`` JSON.
The optional ``?include=...,nls`` is honoured by the controller as a
*reference key* on each nodedef (``nls`` field), but the NLS string table
is **not** returned and is not needed by pyisyox — every visible string is
already inline-resolved in property/command ``name`` fields and in WS
event ``<fmtName>``/``<fmtAct>`` elements.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from pyisyox.schema.editor import Editor
from pyisyox.schema.linkdef import LinkDef
from pyisyox.schema.nodedef import NodeDef


@dataclass(slots=True)
class Instance:
    """One instance within a family — a self-contained set of editors,
    linkdefs, and nodedefs.

    For built-in families there is typically one instance with id ``"1"``.
    For PG3 plugin families (family ``"10"`` in the captured fixture), the
    instance id matches the plugin slot number and is encoded in node
    addresses as the ``n0XX_`` prefix.
    """

    id: str
    name: str
    editors: dict[str, Editor] = field(default_factory=dict)
    linkdefs: dict[str, LinkDef] = field(default_factory=dict)
    nodedefs: dict[str, NodeDef] = field(default_factory=dict)


@dataclass(slots=True)
class Family:
    """A family of instances. Family id is a string so plugin slots
    (``"10"``, ``"11"``, …) and the special ``"common"`` family can coexist.
    """

    id: str
    name: str
    instances: dict[str, Instance] = field(default_factory=dict)


@dataclass(slots=True)
class Profile:
    """The decoded result of one ``/rest/profiles`` JSON blob.

    The ``nodedef_lookup`` is the load-bearing artifact callers use to
    resolve a node (which carries ``family_id``, ``instance_id``, and
    ``nodeDefId``) to its :class:`NodeDef`.
    """

    timestamp: str = ""
    families: dict[str, Family] = field(default_factory=dict)
    nodedef_lookup: dict[tuple[str, str, str], NodeDef] = field(default_factory=dict)

    @classmethod
    def load_from_json(cls, raw: dict) -> Profile:
        """Build a :class:`Profile` from a parsed ``/rest/profiles`` response.

        Args:
            raw: Top-level dict with ``timestamp`` and ``families[]``.

        Returns:
            A populated :class:`Profile` with families, instances, and a
            built lookup table.
        """
        profile = cls(timestamp=str(raw.get("timestamp", "")))

        for fam_raw in raw.get("families", []) or []:
            family_id = str(fam_raw.get("id", ""))
            family = Family(id=family_id, name=fam_raw.get("name", ""))

            for inst_raw in fam_raw.get("instances", []) or []:
                instance_id = str(inst_raw.get("id", ""))
                instance = Instance(id=instance_id, name=inst_raw.get("name", ""))

                for ed_raw in inst_raw.get("editors", []) or []:
                    editor = Editor.from_json(ed_raw)
                    instance.editors[editor.id] = editor

                for ld_raw in inst_raw.get("linkdefs", []) or []:
                    linkdef = LinkDef.from_json(ld_raw)
                    instance.linkdefs[linkdef.id] = linkdef

                for nd_raw in inst_raw.get("nodedefs", []) or []:
                    nodedef = NodeDef.from_json(nd_raw, family_id=family_id, instance_id=instance_id)
                    instance.nodedefs[nodedef.id] = nodedef
                    profile.nodedef_lookup[nodedef.lookup_key] = nodedef

                family.instances[instance_id] = instance

            profile.families[family_id] = family

        return profile

    def find_nodedef(self, nodedef_id: str, family_id: str, instance_id: str) -> NodeDef | None:
        """Resolve a nodedef by its ``(id, family, instance)`` join key.

        Returns ``None`` when no matching nodedef exists — callers should
        treat that as the unknown-type case (e.g. fall back to the
        nodedef-driven HA platform classifier rather than the type-based one).
        """
        return self.nodedef_lookup.get((nodedef_id, family_id, instance_id))

    def find_editor(self, editor_id: str, family_id: str, instance_id: str) -> Editor | None:
        """Resolve an editor by id within a family/instance scope.

        Editors are scoped to their instance — the same id (e.g. ``"bool"``)
        may appear in multiple instances with different ranges, so the
        family/instance must be supplied.
        """
        family = self.families.get(family_id)
        if family is None:
            return None
        instance = family.instances.get(instance_id)
        if instance is None:
            return None
        return instance.editors.get(editor_id)
