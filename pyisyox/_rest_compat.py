"""EXPERIMENT — can a generic XML→"API JSON shape" transform let the
``:8443/rest/*`` legacy-XML surface feed the existing ``/api/*`` JSON
parsers, instead of rebuilding endpoint-specific XML parsers?

Background: ``LocalAuth`` (HTTP basic) only reaches the eisy's ``:8443``
listener, which has the ``/rest/*`` family (incl. the *modern*
``/rest/profiles?include=`` JSON blob, ``/rest/status``, ``/rest/nodes``,
``/rest/programs``, ``/rest/vars/get/{type}``, ``/rest/subscribe``) but
**no** ``/api/*`` endpoints. The v6 client loads config/nodes/programs/
triggers/variables from ``/api/*``. This module tests whether a thin
generic transform — like ``runtime.events._xml_to_obj`` but shaped to
match the IoX JSON convention (attributes as plain keys, element text
under ``"_"`` when the element also has attrs/children, repeated tags →
list) — plus a one-line per-endpoint adapter can produce something the
existing ``parse_api_*`` functions accept unchanged.

Run as a script against a controller:
    python -m pyisyox._rest_compat https://eisy.local:8443 admin admin
"""

from __future__ import annotations

from typing import Any
from xml.etree import ElementTree as ET


def rest_xml_to_obj(el: ET.Element) -> Any:
    """Recursively turn an element into an IoX-JSON-shaped value.

    Mirrors the JSON convention the controller uses on ``/api/*``:

    * attributes become plain dict keys (no ``@`` prefix)
    * child elements become dict keys; repeated tags collapse to a list
    * text content of an element that *also* has attrs/children goes
      under ``"_"`` (the IoX convention, e.g. ``family: {"_": "1",
      "instance": "1"}``)
    * a leaf element is just its text string
    * an empty element (``<root/>``) → ``""``
    """
    obj: dict[str, Any] = {k: v for k, v in el.attrib.items()}
    for child in el:
        val = rest_xml_to_obj(child)
        tag = child.tag
        if tag in obj:
            existing = obj[tag]
            if isinstance(existing, list):
                existing.append(val)
            else:
                obj[tag] = [existing, val]
        else:
            obj[tag] = val
    text = (el.text or "").strip()
    if not obj:
        return text  # leaf: the element IS its text (or "" if empty)
    if text:
        obj["_"] = text
    return obj


def _aslist(v: Any) -> list:
    if v is None or v == "":
        return []
    return v if isinstance(v, list) else [v]


def _root_obj(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)  # noqa: S314 — eisy LAN traffic
    out = rest_xml_to_obj(root)
    return out if isinstance(out, dict) else {}


# --- per-endpoint adapters: reshape the generic transform into exactly
#     what the existing parse_api_* functions expect ---------------------


def nodes_xml_to_api_json(xml_text: str) -> dict[str, Any]:
    """``GET :8443/rest/nodes`` (XML) → the ``/api/nodes`` JSON shape
    (``{"data": {"nodes": {"node": [...]}}}``).

    ``/rest/nodes`` carries ``<root>``, ``<group>``, ``<folder>`` and
    ``<node>`` siblings; ``/api/nodes`` is node-only (groups/folders come
    from the same ``/rest/nodes`` XML separately, which the client
    already parses). So we just lift out the ``<node>`` list.

    ``<node>`` in the legacy XML carries ``flag`` / ``nodeDefId`` as
    *attributes* and ``address`` / ``name`` / ``type`` / ``enabled`` /
    ``pnode`` / ``parent`` / ``family`` as child elements — which
    ``rest_xml_to_obj`` already turns into ``{"flag": ..., "nodeDefId":
    ..., "address": ..., "parent": {"type": ..., "_": ...}, "family":
    {"instance": ..., "_": ...}, ...}`` — i.e. exactly the ``/api/nodes``
    element shape ``_node_from_api_json`` consumes (``family["_"]``,
    ``parent["_"]``, ``enabled`` string, ``flag`` string, …). No
    ``<property>`` children on ``/rest/nodes`` → ``merge_status_into_nodes``
    fills those from ``/rest/status`` exactly as it does for ``/api/nodes``
    plugin nodes.
    """
    obj = _root_obj(xml_text)  # the <nodes> element
    return {"data": {"nodes": {"node": _aslist(obj.get("node"))}}}


def config_xml_to_api_json(xml_text: str) -> dict[str, Any]:
    """``GET :8443/rest/config`` (XML) → the ``/api/config`` JSON shape
    (``{"data": {"uuid": ..., "version": ..., "portalHost": ...}}``).

    Not a clean generic mapping — the legacy ``<configuration>`` has uuid
    at ``root/id`` and version at ``app_version`` (``portalHost`` isn't
    in ``/rest/config``; it's portal-only and unused by LocalAuth). So
    this one needs a 3-field dig rather than a passthrough.
    """
    obj = _root_obj(xml_text)  # the <configuration> element
    root = obj.get("root")
    uuid = root.get("id", "") if isinstance(root, dict) else ""
    version = obj.get("app_version") or obj.get("app_full_version") or ""
    return {"data": {"uuid": uuid, "version": version, "portalHost": None}}


def programs_xml_to_api_json(xml_text: str) -> dict[str, Any]:
    """``GET :8443/rest/programs`` (XML) → roughly the ``/api/programs``
    list shape. ``<program id="0001" folder="false" enabled="true"
    runAtStartup="false" running="idle"><name>…</name>…</program>`` →
    ``{"id": "0001", "folder": "false", ...}`` via the generic transform;
    whether ``parse_api_programs`` accepts that verbatim depends on key
    names (``folder`` vs ``isFolder``, ``runAtStartup`` vs ``runAtReboot``,
    nested ``<id>`` vs attribute, …) — printed by the demo so we can see
    the delta. ``/api/programs`` additionally returns the parsed AST
    (``/api/triggers``) which the legacy ``/rest/programs/{id}`` doesn't —
    that part has no XML equivalent.
    """
    obj = _root_obj(xml_text)  # the <programs> element
    return {"data": _aslist(obj.get("program"))}


# --- demo runner ---------------------------------------------------------


def _demo(base_url: str, user: str, pw: str) -> None:  # pragma: no cover - script
    import json

    import aiohttp  # noqa: PLC0415

    async def run() -> None:
        from pyisyox.client import (  # noqa: PLC0415
            merge_status_into_nodes,
            parse_api_nodes,
            parse_api_programs,
            parse_rest_status,
        )

        async with aiohttp.ClientSession(
            auth=aiohttp.BasicAuth(user, pw),
            connector=aiohttp.TCPConnector(ssl=False),
        ) as s:

            async def get(path: str) -> str:
                async with s.get(f"{base_url}{path}") as r:
                    print(f"  GET {path} -> {r.status}")
                    return await r.text()

            print("\n== /rest/config -> /api/config shape ==")
            cfg_json = config_xml_to_api_json(await get("/rest/config"))
            print("  ", cfg_json)

            print("\n== /rest/nodes -> /api/nodes shape ==")
            nodes_xml = await get("/rest/nodes")
            api_json = nodes_xml_to_api_json(nodes_xml)
            print("  transformed:", json.dumps(api_json)[:400])
            recs = parse_api_nodes(api_json)
            print(f"  parse_api_nodes -> {len(recs)} record(s): {list(recs)[:5]}")
            status_xml = await get("/rest/status")
            merge_status_into_nodes(recs, parse_rest_status(status_xml))
            print(f"  after merge_status: {[(a, list(r.properties)) for a, r in list(recs.items())[:5]]}")

            print("\n== /rest/programs -> /api/programs shape ==")
            prog_xml = await get("/rest/programs")
            print("  raw xml:", prog_xml[:300] or "(empty)")
            prog_json = programs_xml_to_api_json(prog_xml) if prog_xml.strip() else {"data": []}
            print("  transformed:", json.dumps(prog_json)[:300])
            try:
                progs = parse_api_programs(prog_json["data"])
                print(f"  parse_api_programs -> {len(progs)} record(s)")
            except Exception as e:  # noqa: BLE001
                print(f"  parse_api_programs raised: {type(e).__name__}: {e}")

            print("\n== synthetic populated <node> (modelled on classic IoX) ==")
            synth = (
                "<nodes><root></root>"
                '<node flag="128" nodeDefId="DimmerLampSwitch">'
                "<address>3D 7D 87 1</address><name>Hall Lamp</name>"
                '<parent type="1">3D 7D 87</parent>'
                "<type>1.32.65.0</type><enabled>true</enabled><pnode>3D 7D 87 1</pnode>"
                '<family instance="1">1</family></node>'
                '<node flag="0" nodeDefId="flume2"><address>n010_abc</address>'
                "<name>Flume</name><parent type=\"1\">n010_controller</parent>"
                "<type>1.2.3.4</type><enabled>true</enabled><pnode>n010_controller</pnode>"
                '<family instance="10">10</family></node></nodes>'
            )
            sj = nodes_xml_to_api_json(synth)
            print("  transformed:", json.dumps(sj))
            sr = parse_api_nodes(sj)
            for a, r in sr.items():
                print(
                    f"   {a!r}: nodedef={r.nodedef_id!r} fam={r.family_id}/{r.instance_id} "
                    f"type={r.type!r} parent={r.parent_address!r} pnode={r.pnode!r} "
                    f"enabled={r.enabled} flag={r.flag}"
                )

    import asyncio  # noqa: PLC0415

    asyncio.run(run())


if __name__ == "__main__":  # pragma: no cover - script
    import sys

    _demo(sys.argv[1], sys.argv[2], sys.argv[3])
