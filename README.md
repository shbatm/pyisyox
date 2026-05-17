# pyisyox

A modern async Python library for [Universal Devices](https://www.universal-devices.com/) **eisy** and **Polisy** home controllers (IoX firmware 6.0.0+).

Connect to your controller, read and control everything it manages — Insteon, Z-Wave, Zigbee/Matter, scenes, programs, and variables — and stay in sync over a live WebSocket stream.

> **Public beta.** The API is stabilizing ahead of the 6.0.0 release. Small breaking changes are still possible between betas, so pin an exact version when you depend on it.

## Status & scope

- **Works with:** eisy or Polisy running IoX 6.0.0 or newer.
- **Not for ISY-994:** the original ISY-994 hardware (and pre-6.0 firmware) is out of scope — use the upstream [`pyisy`](https://pypi.org/project/pyisy/) v3.x library, which still backs Home Assistant Core's official `isy994` integration.
- **Home Assistant:** [`hacs-udi-iox`](https://github.com/shbatm/hacs-udi-iox) is a HACS custom component that wraps this library for eisy on IoX 6+. You can also use pyisyox directly as an async library or through the bundled CLI.

## Highlights

- JSON-first connection flow against `/api/*` endpoints with a one-shot `/rest/status` overlay merge — ≤ 8 HTTP + 1 WS regardless of node-server count.
- Two auth strategies behind a single `Auth` protocol:
  - **`PortalAuth`** — JWT bearer from `POST /api/login` with proactive refresh + best-effort logout. Recommended default; works fully offline (eisy validates locally).
  - **`LocalAuth`** — HTTP basic against `:8443/rest/*`. Feature-degraded fallback (no `/api/triggers` AST, no `/api/variables` names).
- Editor-codec-validated `Node.send_command` — enum names, subset constraints, and range bounds caught before any HTTP hits the wire.
- PG3 plugin parity: native and plugin nodes share one `NodeDef` shape; the platform classifier handles both uniformly.
- WebSocket event dispatcher with auto-reconnect, surfacing both property updates and a typed `NodeLifecycleEvent` channel for plugin add/remove/rename.
- `Profile.merge` for in-place dynamic-profile reload — runtime objects keep their references valid.

## Install

```bash
pip install pyisyox
```

Requires Python 3.11+. During beta, `pip install pyisyox` resolves to the latest beta; pin an exact version (e.g. `pyisyox==6.0.0b6`) for reproducible builds.

## Quickstart

```python
import asyncio
from pyisyox import Controller, PortalAuth

async def main():
    controller = Controller(
        "https://eisy.local:443",
        PortalAuth("you@example.com", "portal-password"),
    )
    await controller.connect()
    try:
        # Read state
        node = controller.nodes["3D 7D 87 1"]
        print(node.name, "=", node.properties["ST"].formatted)

        # Send a command (validated against the nodedef's editors first)
        await node.send_command("DON", 75)  # 75% on-level

        # Subscribe to live updates
        controller.add_event_listener(
            lambda ev: print(f"{ev.node_address}.{ev.control} = {ev.formatted_action}")
        )
        await asyncio.sleep(60)
    finally:
        await controller.stop()

asyncio.run(main())
```

For local-admin (basic auth) mode:

```python
from pyisyox import Controller, LocalAuth
controller = Controller("https://eisy.local:8443", LocalAuth("admin", "password"))
```

A smoke-test CLI is bundled — picks the auth mode from the username (email → `PortalAuth`, otherwise `LocalAuth`):

```bash
python3 -m pyisyox https://eisy.local:443 you@example.com portal-password
python3 -m pyisyox https://eisy.local:8443 admin local-password
```

Pass `--dump <path>` to write a full controller snapshot (every node, group, program, variable, network resource, plus the loaded profile and WS health) as pretty-printed JSON — handy when filing a bug report or diffing controller state between firmware versions:

```bash
python3 -m pyisyox https://eisy.local:443 you@example.com pw --no-events --dump ~/snapshots/eisy.json
```

The snapshot is produced by `Controller.to_dict()`; every runtime class (`Node`, `Group`, `Folder`, `Program`, `Variable`, `NetworkResource`, `Profile`) also exposes a `.to_dict()` so embedding consumers can serialise individual objects.

## Public surface

```text
pyisyox.Controller                         — top-level handle
pyisyox.PortalAuth / LocalAuth / Auth      — auth strategies
pyisyox.Node                                — runtime device handle
pyisyox.Group / Folder                     — IoX scenes + organisational tree
pyisyox.Event / EventDispatcher            — WebSocket event types
pyisyox.NodeLifecycleEvent / NodeLifecycleAction
                                            — typed _3 ND/NR/RG channel
pyisyox.classify(nodedef)                  — HA platform routing classifier
pyisyox.Profile / ProfileMergeResult       — schema + merge for dynamic reload
pyisyox.Editor / NodeDef / Command          — schema dataclasses
pyisyox.IoXClient                          — lower-level HTTP client (rare; Controller handles this)
```

`Controller` is the only thing most consumers need to construct. It composes everything else internally.

## Common tasks

### Send a command with validation

```python
node = controller.nodes["3D 7D 87 1"]
await node.send_command("DON", 75)         # KeypadDimmer: I_OL editor enforces 0..100
await node.send_command("CLIMD", "Heat")    # Thermostat: enum-name resolved via editor codec
```

### Set a variable

```python
await controller.set_variable_value(2, 8, 42)        # state var #8 → 42
await controller.set_variable_init(2, 8, 1)          # restore-on-startup default
await controller.rename_variable(2, 8, "DoorState")
```

### React to a plugin reload

```python
def on_lifecycle(ev):
    if ev.requires_reload:
        # Surface a "reload integration" prompt to the user, or just refresh:
        asyncio.create_task(controller.refresh())

controller.add_node_lifecycle_listener(on_lifecycle)
```

### HA platform routing for unknown nodedefs

Native devices route via type strings (HA Core's existing logic). Plugin nodedefs route via `pyisyox.classify`:

```python
from pyisyox import classify
nodedef = controller.profile.find_nodedef("flume2", "10", "10")
result = classify(nodedef, find_editor=lambda eid: controller.profile.find_editor(eid, "10", "10"))
# result.controllable, .triggers, .buttons, .readings — direct map to HA platforms
```

## Architecture

- **Schema** (`pyisyox.schema`) — vendored from UDI's nucore-ai source. NodeDef / Editor / Command / LinkDef / UOM dataclasses + `Profile.load_from_json` + the `(nodedef_id, family_id, instance_id)` lookup. Editors carry a bidirectional codec used both for decoding property values and validating outbound command parameters.
- **Auth** (`pyisyox.auth`) — `Auth` protocol + concrete `PortalAuth` (JWT bearer with proactive refresh + best-effort logout) and `LocalAuth` (HTTP basic). Lock-protected token state for safe concurrent use.
- **Client** (`pyisyox.client`) — JSON-first HTTP client, parallel initial-load orchestrator, narrow XML decoders for the three remaining XML surfaces (`/rest/status`, `/rest/nodes/{addr}/cmd/...` responses, `/rest/subscribe` event frames).
- **Runtime** (`pyisyox.runtime`) — Node / Group / Folder wrappers, EventDispatcher, WebSocketEventStream with auto-reconnect.
- **Classifier** (`pyisyox.classifier`) — three-axis HA platform classifier as a fallback for unknown nodedefs (controllable + triggers + buttons + readings).
- **Controller** (`pyisyox.controller`) — top-level glue. Owns the lifecycle (connect / refresh / stop), exposes nodes/groups/folders/programs/triggers/variables, surfaces event + status + lifecycle subscriptions.

## Lineage

Originated from [PyISY](https://github.com/automicus/PyISY), authored by Ryan Kraus and maintained by Greg Laabs. PyISY v3.x continues to support the original ISY-994 hardware family and is what Home Assistant Core's `isy994` integration depends on. **pyisyox is a from-scratch rewrite** by [@shbatm] for IoX 6+ — different API, different scope, different consumer. Do not import pyisyox patterns into PyISY.

## Development

```bash
pip install -r requirements.txt -r requirements-dev.txt
pip install -e .
pre-commit install
pre-commit run --all-files
pytest
```

Tests run fully offline against captured (anonymized) eisy fixtures under `tests/fixtures/eisy6/`. Anything new committed to that directory must go through the scrubber that strips Insteon device prefixes, JWTs, MACs, emails, and lat/long — see the fixture-anonymization regression tests.

## License

Apache 2.0. See `LICENSE.txt`.

[@shbatm]: https://github.com/shbatm
