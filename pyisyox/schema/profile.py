"""Profile loader for IoX ``/rest/profiles`` JSON responses.

Parses one profile blob (families → instances → editors/linkdefs/nodedefs)
into resolved Python objects, then builds the
``(nodedef_id, family_id, instance_id) → NodeDef`` lookup that lets a node
from ``/api/nodes`` find its definition.

This module knows the JSON wire shape but does no HTTP. Callers pass
already-fetched dicts to :meth:`Profile.load_from_json`.

Source schema: ``/rest/profiles?include=nodedefs,editors,linkdefs`` JSON.
For its families the controller inline-resolves every visible string into
the ``name`` fields, so the NLS string table isn't needed there. The
*dynamically* generated Z-Wave nodedefs (fetched from ``def/get`` XML, not
``/rest/profiles``) are the exception: their commands/properties arrive
label-less, so :mod:`pyisyox.client` fetches the relevant per-family NLS
tables and stashes the merged result on :attr:`Profile.nls` — used to
resolve those labels and to fill encoded editors' enum option names.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from pyisyox.schema.editor import Editor
from pyisyox.schema.linkdef import LinkDef
from pyisyox.schema.nls import NLSTable
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
    #: Merged NLS string table for any dynamically-loaded families (Z-Wave).
    #: Empty unless :mod:`pyisyox.client` fetched per-family NLS during load.
    nls: NLSTable = field(default_factory=NLSTable)

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

    def merge(self, other: Profile) -> ProfileMergeResult:
        """Merge ``other`` into ``self`` in place; return a diff summary.

        Designed for PG3 dynamic profile reload: when a plugin updates
        its nodedefs at runtime (per
        https://developer.isy.io/docs/API/pg/DynamicProfiles), the
        consumer refetches ``/rest/profiles`` and constructs a fresh
        :class:`Profile` from the new payload, then calls this method
        to fold the updates into the live one. Existing
        :class:`pyisyox.runtime.Node` instances hold a reference to
        the resolved :class:`NodeDef`; if we replaced the Profile
        wholesale they'd cling to stale lookups, so the merge mutates
        the existing dicts and replaces individual NodeDef / Editor /
        LinkDef objects rather than rebuilding the structure.

        Semantics:

        * Editors / LinkDefs / NodeDefs in ``other`` overwrite same-id
          entries in ``self`` at the same family/instance scope.
        * Editors / LinkDefs / NodeDefs only in ``self`` are kept —
          ``other`` is treated as additive, never as a delete list.
          (To remove items, the caller passes a profile with the
          removed entries explicitly absent and tracks the
          :attr:`ProfileMergeResult` to act on the diff themselves.)
        * Families / Instances new to ``other`` are added to ``self``.
        * The ``nodedef_lookup`` table is updated so the
          ``(nodedef_id, family_id, instance_id)`` join key resolves
          to the new instance.

        Returns:
            A :class:`ProfileMergeResult` listing the nodedef ids that
            were added vs replaced, plus the equivalent for editors
            and linkdefs. Consumers can use this to invalidate caches
            or re-classify nodes whose nodedef changed.
        """
        result = ProfileMergeResult()
        for fam_id, other_family in other.families.items():
            self_family = self.families.get(fam_id)
            if self_family is None:
                self.families[fam_id] = other_family
                for inst in other_family.instances.values():
                    for nd in inst.nodedefs.values():
                        self.nodedef_lookup[nd.lookup_key] = nd
                        result.nodedefs_added.append(nd.lookup_key)
                continue

            for inst_id, other_inst in other_family.instances.items():
                self_inst = self_family.instances.get(inst_id)
                if self_inst is None:
                    self_family.instances[inst_id] = other_inst
                    for nd in other_inst.nodedefs.values():
                        self.nodedef_lookup[nd.lookup_key] = nd
                        result.nodedefs_added.append(nd.lookup_key)
                    continue

                _merge_instance(self_inst, other_inst, self.nodedef_lookup, result)

        if other.timestamp:
            self.timestamp = other.timestamp
        return result

    def register_nodedefs(self, family_id: str, instance_id: str, nodedefs: dict[str, NodeDef]) -> None:
        """Add a batch of nodedefs to ``family_id``/``instance_id`` in place.

        Used for the dynamically-generated Z-Wave nodedefs, which aren't
        carried by ``/rest/profiles`` and are fetched separately from
        ``/rest/zwave/node/{addr}/def/get``. The target family / instance
        is created if it doesn't exist yet (the Z-Wave families *do*
        already exist in ``/rest/profiles`` — with their ``ZW_*`` editors
        but no nodedefs — so usually only the nodedef dicts are filled
        in). Existing same-id nodedefs at that scope are overwritten and
        the ``nodedef_lookup`` table updated so the
        ``(nodedef_id, family_id, instance_id)`` join key resolves.
        """
        family = self.families.setdefault(family_id, Family(id=family_id, name=""))
        instance = family.instances.setdefault(instance_id, Instance(id=instance_id, name=""))
        for nodedef in nodedefs.values():
            instance.nodedefs[nodedef.id] = nodedef
            self.nodedef_lookup[nodedef.lookup_key] = nodedef

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

        An *encoded editor id* — one that fully describes its range, e.g.
        ``"_51_0_R_0_101_N_IX_DIM_REP"`` — is decoded directly via
        :meth:`Editor.from_encoded_id`; this is how the dynamically-
        generated Z-Wave nodedefs spell most of their editors. (The check
        is "does it parse as an encoding", not just "starts with ``_``" —
        UDI also ships *named* editors that begin with ``_`` such as
        ``_sys_notify_full``, which fall through to the table lookup.)

        Table-lookup fallback chain on miss:

        1. ``family_id`` / ``instance_id`` (the requested scope)
        2. The ``"common"`` family / instance ``"1"`` — UDI publishes a
           shared set of editors there (``_sys_notify_full``, etc.) that
           any plugin nodedef can reference

        Returns ``None`` if it's neither a valid encoding nor present in
        either scope.
        """
        if editor_id.startswith("_"):
            encoded = Editor.from_encoded_id(editor_id)
            if encoded is not None:
                for rng in encoded.ranges:
                    if rng.nls_prefix and not rng.names:
                        rng.names = self.nls.enum_names(rng.nls_prefix)
                return encoded
        editor = self._editor_in(family_id, instance_id, editor_id)
        if editor is not None:
            return editor
        return self._editor_in("common", "1", editor_id)

    def _editor_in(self, family_id: str, instance_id: str, editor_id: str) -> Editor | None:
        family = self.families.get(family_id)
        if family is None:
            return None
        instance = family.instances.get(instance_id)
        if instance is None:
            return None
        return instance.editors.get(editor_id)

    def to_dict(self) -> dict[str, Any]:
        """Flatten the profile to a JSON-compatible dict.

        :attr:`nodedef_lookup` is dropped — its
        ``(nodedef_id, family_id, instance_id)`` tuple keys are not
        JSON-encodable and the same data lives under
        ``families[fam].instances[inst].nodedefs``. ``nodedef_lookup_count``
        is surfaced as a sanity-check counter.

        :class:`pyisyox.schema.editor.EditorRange` carries
        ``subset: set[int]`` which JSON can't encode either; the
        walker below normalises every set into a sorted list so the
        snapshot round-trips through ``json.dumps``.
        """
        return {
            "timestamp": self.timestamp,
            "families": _jsonify({family_id: asdict(family) for family_id, family in self.families.items()}),
            "nls": asdict(self.nls),
            "nodedef_lookup_count": len(self.nodedef_lookup),
        }


def _jsonify(value: Any) -> Any:
    """Recursively normalise ``asdict`` output so :mod:`json` can encode it.

    The schema dataclasses carry ``set[int]`` (``EditorRange.subset``)
    which JSON rejects; this walker converts sets to sorted lists,
    keeps dicts / lists / tuples flat, and passes scalars through.
    Kept private — only the ``to_dict`` paths need it.
    """
    if isinstance(value, dict):
        return {key: _jsonify(val) for key, val in value.items()}
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_jsonify(item) for item in value]
    return value


@dataclass(slots=True)
class ProfileMergeResult:
    """Diff produced by :meth:`Profile.merge`.

    Attributes:
        nodedefs_added: ``(nodedef_id, family_id, instance_id)`` triples
            for nodedefs that didn't exist in the destination profile
            before the merge.
        nodedefs_replaced: Same shape, for nodedefs whose existing
            entry was overwritten with a fresh :class:`NodeDef`. The
            old object is no longer in ``Profile.nodedef_lookup``;
            consumers caching it should refresh.
        editors_added / editors_replaced: ``(editor_id, family_id,
            instance_id)`` triples for editors.
        linkdefs_added / linkdefs_replaced: same for linkdefs.
    """

    nodedefs_added: list[tuple[str, str, str]] = field(default_factory=list)
    nodedefs_replaced: list[tuple[str, str, str]] = field(default_factory=list)
    editors_added: list[tuple[str, str, str]] = field(default_factory=list)
    editors_replaced: list[tuple[str, str, str]] = field(default_factory=list)
    linkdefs_added: list[tuple[str, str, str]] = field(default_factory=list)
    linkdefs_replaced: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        """True when *anything* differed — nodedefs, editors, or linkdefs."""
        return bool(
            self.nodedefs_added
            or self.nodedefs_replaced
            or self.editors_added
            or self.editors_replaced
            or self.linkdefs_added
            or self.linkdefs_replaced
        )


def _merge_instance(
    self_inst: Instance,
    other_inst: Instance,
    nodedef_lookup: dict[tuple[str, str, str], NodeDef],
    result: ProfileMergeResult,
) -> None:
    """Per-instance merge — editors, linkdefs, nodedefs.

    Replacement is reported only when the incoming entry differs from
    the existing one (dataclass equality). Identical content is a
    no-op so consumers polling refresh() on a quiet controller see
    ``changed is False``.
    """
    for ed_id, ed in other_inst.editors.items():
        key = (ed_id, self_inst.id, self_inst.id)
        existing = self_inst.editors.get(ed_id)
        if existing is None:
            result.editors_added.append(key)
            self_inst.editors[ed_id] = ed
        elif existing != ed:
            result.editors_replaced.append(key)
            self_inst.editors[ed_id] = ed

    for ld_id, ld in other_inst.linkdefs.items():
        key = (ld_id, self_inst.id, self_inst.id)
        existing_ld = self_inst.linkdefs.get(ld_id)
        if existing_ld is None:
            result.linkdefs_added.append(key)
            self_inst.linkdefs[ld_id] = ld
        elif existing_ld != ld:
            result.linkdefs_replaced.append(key)
            self_inst.linkdefs[ld_id] = ld

    for nd_id, nd in other_inst.nodedefs.items():
        existing_nd = self_inst.nodedefs.get(nd_id)
        if existing_nd is None:
            result.nodedefs_added.append(nd.lookup_key)
            self_inst.nodedefs[nd_id] = nd
            nodedef_lookup[nd.lookup_key] = nd
        elif existing_nd != nd:
            result.nodedefs_replaced.append(nd.lookup_key)
            self_inst.nodedefs[nd_id] = nd
            nodedef_lookup[nd.lookup_key] = nd
