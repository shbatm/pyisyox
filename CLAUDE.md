# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**PyISYoX** is an async Python client for Universal Devices' **eisy** /
**Polisy** controllers running **IoX 6.0.0+**. It connects, loads the
device tree (nodes, scenes, programs, variables, network resources),
and keeps it live via a WebSocket event stream.

- **In scope:** eisy / Polisy on IoX 6+. Insteon, X10, Z-Wave,
  Zigbee/Matter (whatever the hardware supports).
- **Out of scope:** original ISY-994 hardware and pre-6.0 firmware —
  consumers needing those should use the upstream
  [`pyisy`](https://github.com/automicus/PyISY) (v3.x) library.

**Lineage:** rewritten by [@shbatm](https://github.com/shbatm) from
[PyISY](https://github.com/automicus/PyISY) (Ryan Kraus & Greg Laabs),
driven by the needs of the Home Assistant ISY integration. No
affiliation with Universal Devices, Inc.

> **Branch note:** `main` still carries the legacy v3.x-shaped layout
> (`isy.py`, `connection.py`, `nodes/`, `helpers/xml.py`, …). **The v6
> rewrite lives on `dev`** and that is where PRs go. The repo's default
> branch is `main`, so a `Closes #N` line in a PR body does **not**
> auto-close the issue on merge — close referenced issues manually
> after merging to `dev`. The maintainer rebase-merges
> (`gh pr merge N --rebase`) to keep per-commit history.

## Requirements

- **Python 3.10+**
- **Dependencies:** `aiohttp`, `python-dateutil`, `requests`,
  `colorlog`, `xmltodict`

## Development Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt -r requirements.txt
pip install -e .
pre-commit install
```

> **Worktree gotcha:** pre-commit's `mypy` / `pylint` hooks run via
> `script/run-in-env.sh`, which activates `./.venv/bin/activate` if it
> exists (else falls back to `/opt/venv/bin/`). A fresh `git worktree`
> has no `.venv`, so those hooks fail with `command not found` and the
> commit aborts. Create a `.venv` in the worktree once (the block
> above) to fix it.

### Smoke-testing against a real controller

```bash
# Portal (JWT) auth is selected when the username looks like an email:
python3 -m pyisyox https://eisy.local:443 me@example.com 'password'
# Local (HTTP basic) auth otherwise; -q skips the WebSocket stream:
python3 -m pyisyox https://eisy.local:8443 admin 'password' -q
```

`pyisyox/__main__.py` is a deliberately tiny CLI for ad-hoc
verification — not the consumer API. Real consumers construct
`pyisyox.Controller` directly.

### Tests, linting, type-checking

```bash
pytest                       # or: pytest --cov=pyisyox
ruff check pyisyox --fix
ruff format pyisyox
mypy pyisyox
pylint pyisyox
```

Pre-commit also runs `codespell`, `yamllint`, and `prettier`. CI on a
PR: `Run tests (3.11 / 3.13 / 3.14)`, `pre-commit`, and `claude-review`
(the review takes ~4–5 min; the rest ~30–40 s each).

### Docs

Sphinx sources under `docs/`; build with `cd docs && make html`
(needs `pip install -r docs/requirements.txt`). Regenerate the API
stubs with `make apidoc`. Docs are **not** on CI; ~200 suppressed
warnings are expected. Published at
<https://pyisyox.readthedocs.io>.

## Architecture

The public surface is intentionally small and layered. From most to
least "glue":

### `Controller` (`controller.py`) — the top-level handle

- Constructed with a base URL + an `Auth` strategy (no network calls
  yet): `Controller(url, PortalAuth(email, pw))`.
- `await controller.connect()` — validates the connection
  (`/rest/config`), loads the profile, loads all platforms in parallel,
  and (unless `start_websocket=False`) starts the WebSocket reader.
- `await controller.stop()` — stops the WS, best-effort logout, closes
  the session if owned. Idempotent.
- **Read accessors** (populated by the load, mutated by the
  dispatcher): `.config`, `.profile`, `.nodes` (`dict[str, Node]`),
  `.groups`, `.folders`, `.programs`, `.program_folders`, `.triggers`,
  `.variables` (`dict[type, dict[id, Variable]]`),
  `.network_resources`, `.connected`, `.websocket`, `.base_url`.
- **Mutators:** `send_program_command`, `run_network_resource`,
  `set_variable_value` / `set_variable_init` / `rename_variable`,
  `rename_node` / `rename_group` / `rename_folder`.
- **Event subscription:** `add_event_listener`,
  `add_status_listener`, `add_node_lifecycle_listener`,
  `add_program_status_listener` — each returns an unsubscribe callable.
- **Refresh:** `refresh()` re-runs the full load;
  `refresh_profile()` re-fetches `/rest/profiles` and reports a
  `ProfileMergeResult`.
- **Testing seam:** `feed_event_frame(raw)` pushes a raw WS frame
  through the dispatcher without a live socket.

See [`docs/connection-flow.md`](docs/connection-flow.md) for the full
connect sequence, endpoint list, retry logic, and event routing.

### Auth strategies (`auth.py`)

- `PortalAuth(email, password)` — JWT bearer; the **recommended
  default**. Logs in against the UD portal, refreshes the access token
  as needed, posts `/api/jwt/logout` on close.
- `LocalAuth(username, password)` — HTTP basic; a feature-degraded
  fallback. No logout.
- `Auth` — the shared base / protocol.
- `AuthError` on failure.

### HTTP client + load records (`client.py`)

- `IoXClient` — the thin REST/WS HTTP layer the `Controller` drives
  internally. Produces the typed record dataclasses below; consumers
  rarely touch it directly.
- Record dataclasses: `ControllerConfig`, `NodeRecord`,
  `NodePropertyValue`, `GroupRecord`, `FolderRecord`, `ProgramRecord`,
  `VariableRecord`, `NetworkResourceRecord`; aggregate `LoadResult`.
- `parse_*` functions turn the REST XML into those records.
- Wire-vocabulary enums used in mutation request bodies: `NodeType`,
  `VariableField`.
- Errors: `ClientError`, `HTTPError`.

### REST / WS paths (`paths.py`)

Every endpoint path is centralised here — fixed paths as string
constants, parametric paths as `.format(...)` templates. The
`Controller` / `IoXClient` use them internally; they're public for
anyone building against the raw wire surface. (This replaced the old
scattered `URL_*` constants in `constants.py`.)

### Runtime wrappers (`runtime/`)

Thin objects that wrap a record + the profile + the client. Each
shares its underlying record with the controller's loaded state, so a
dispatcher update is visible through the wrapper immediately.

- **`Node` (`runtime/node.py`)** — the primary device handle.
  - Structural: `address`, `name`, `nodedef_id`, `family_id`,
    `instance_id`, `type`, `parent_address`, `primary_address`,
    `enabled`, `flag` / `has_flag(...)`, `nodedef`.
  - State: `properties` (`dict[str, NodePropertyValue]`), `status`
    (shortcut to the primary `ST` value).
  - Introspection (all derived from the nodedef / type triple /
    properties — **no hardcoded type-prefix tables**): `protocol`,
    `is_thermostat`, `is_lock`, `is_fan`, `is_dimmable`,
    `is_battery_node`. (No `parent_node` helper — do
    `controller.nodes.get(node.parent_address)`; keeps `Node`
    decoupled from `Controller`.)
  - `send_command(cmd_id, *params)` — looks the command up in the
    node's `NodeDef.cmds.accepts`, validates each param against the
    editor it references via the bidirectional codec in
    `schema/editor.py` (enum names → raw ints; subset / range enforced;
    out-of-range raises **before** any HTTP), then POSTs.
  - Ergonomic wrappers — one-liners over `send_command` (validation
    still goes through the codec): `set_climate_mode`,
    `set_climate_setpoint_heat` / `_cool`, `set_fan_mode`,
    `secure_lock` / `secure_unlock`, `set_on_level`, `set_ramp_rate`,
    `set_backlight`, `start_manual_dimming` / `stop_manual_dimming`,
    `rename`. **Not** included: composite climate setpoint with
    min-gap (HA policy — stays in the consumer) and runtime
    "not a thermostat" guards (let `EditorCodecError` raise).
  - `NodeCommandError` on a command the nodedef doesn't accept.
- **`Group` (`runtime/group.py`)** — an IoX scene.
  `member_addresses`, `controller_addresses`; `group_all_on` /
  `group_any_on` computed on access from the controller's node
  registry — **stateless members (Insteon battery devices: motion
  sensors, RemoteLincs, binary-alarm nodedefs — `nodedef_id` in
  `INSTEON_STATELESS_NODEDEFID`) are skipped** so they don't drag the
  aggregate to False; a member missing from the registry makes
  `group_all_on` False (defensive). `rename`.
- **`Folder` / `ProgramFolder`** — organisational nodes.
- **`Program` (`runtime/program.py`)** + `ProgramCommand` —
  status + `run_then` / `run_else` / `enable` / `disable` etc. via the
  `ProgramCommand` enum.
- **`Variable` (`runtime/variable.py`)** — `value`, `init`,
  `precision`; `set_value` / `set_init` / `rename`.
- **`NetworkResource` (`runtime/network_resource.py`)** — `run()`.

### Event pipeline (`runtime/events.py`, `runtime/ws.py`)

IoX 6 sends `<Event>` frames over a WebSocket with `<control>` (a
property id like `ST`, or an underscore-prefixed system code like
`_7`), `<action>`, `<node>`, and `<eventInfo>`.

- `WebSocketEventStream` — the auto-reconnecting reader; exposes
  health (`status` / `connected` / `last_event_at`).
- `EventDispatcher` — parses frames, updates the matching record, and
  fans out to listeners. `Event` is the parsed frame; lifecycle and
  program-status events have their own types (`NodeLifecycleEvent`,
  `ProgramStatusEvent`).
- **System-event vocabulary** (the _ISY994 Developer Cookbook_ §8.5
  plus IoX-6 additions) as `StrEnum`s, each with a `.label(value)`
  classmethod: `SystemEventControl`, `TriggerAction`,
  `ProgressAction`, `SystemConfigAction`, `InternetAccessStatus`,
  `SecuritySystemAction`, `DeviceLinkerAction`, `NodeLifecycleAction`,
  `DeviceWriteAction` (`_7A`/`_7M` device-write sub-codes that ride
  through on `_7` progress frames). `describe_system_event(control,
action)` renders a frame's pair into a friendly
  `"control_label = action_label"` string.
  - `NODE_LIFECYCLE_EVENT_INFO_TAGS` /
    `DEVICE_WRITE_PROGRESS_EVENT_INFO_TAGS` map each verb/sub-code to
    the `<eventInfo>` child element names it carries — reference
    metadata; pyisyox itself only parses `<node>` on `NODE_ADDED`.
- Listener type aliases (exported for typing): `EventListener`,
  `NodeLifecycleListener`, `ProgramStatusListener`, `StatusListener`.

### Schema (`schema/`)

The decoded `/rest/profiles` blob — nodedefs, editors, commands,
linkdefs, UOMs.

- `Profile` (`schema/profile.py`) — the top-level container, with
  `find_nodedef(...)` / `find_editor(...)`. `ProfileMergeResult`
  reports what changed on a refresh.
- `schema/nodedef.py`, `schema/editor.py`, `schema/cmd.py`,
  `schema/linkdef.py`, `schema/uom.py` — the typed dataclasses.
- `schema/editor.py` carries the **bidirectional value codec**:
  `Editor.encode` validates/encodes a display value to the raw wire
  int (handling `prec`-scaled editors symmetrically with the read-side
  halving), `Editor.decode` goes the other way. `Node.send_command`
  leans on this; out-of-range raises `EditorCodecError`.

### Classifier (`classifier.py`)

`classify(nodedef, ...)` → `ClassificationResult` — a nodedef →
HA-platform fallback classification (`ControllablePlatform` /
`ReadingPlatform` / `Reading`). Used when a node's nodedef doesn't map
to a more specific handler.

### `constants.py`

After the string-constants audit it carries only **live** values plus
**wire-documentation** constants (kept even when currently unused, as
in-code reference for consumers): `CMD_*` command ids, `PROP_*`
property ids, `X10_COMMANDS`, the `UOM_*` tables + `UOM_TO_STATES`,
climate/thermostat tables (`UOM_CLIMATE_MODES`,
`UOM_CLIMATE_MODES_ZWAVE`, `CLIMATE_SETPOINT_MIN_GAP`),
`INSTEON_RAMP_RATES`, `INSTEON_STATELESS_NODEDEFID`,
`BACKLIGHT_SUPPORT` / `BACKLIGHT_INDEX`, the device-address constants,
and the various enums. Endpoint paths now live in `paths.py`, not
here.

### Other modules

- `exceptions.py` — `ISYConnectionError`, `ISYInvalidAuthError`,
  `ISYMaxConnections`, `ISYResponseParseError`, `ISYStreamDataError`,
  `ISYStreamDisconnected`, plus `ControllerNotConnectedError`
  (re-exported from `controller.py`).
- `helpers/session.py` — `build_sslcontext`, `TLSVersionError`
  (eisy ships a self-signed cert; verification is off by default).
- `logging.py` — `enable_logging(level)` and the `LOG_VERBOSE` level.
- `redactor.py` — scrubs secrets from log output.
- `util/` — small internal helpers.

## Usage

```python
from pyisyox import Controller, PortalAuth

async def main():
    controller = Controller("https://eisy.local:443", PortalAuth("me@example.com", "pw"))
    await controller.connect()
    try:
        # Control a device — editor-validated:
        await controller.nodes["3D 7D 87 1"].send_command("DON", 75)
        await controller.nodes["3D 7D 87 1"].set_climate_setpoint_heat(68)

        # Subscribe to all status changes:
        unsub = controller.add_status_listener(lambda evt: print(evt))
        ...
        unsub()
    finally:
        await controller.stop()
```

## Common Workflows

### Adding a runtime command wrapper

1. Add the named command id to `constants.py` (`CMD_*` / `PROP_*`) if
   it isn't already there.
2. Add a one-liner method on `runtime/node.py` (or `group.py`) that
   delegates to `send_command` — no validation logic; the editor codec
   is the source of truth.
3. Add a one-call test in `tests/test_runtime/` asserting the URL it
   produces.

### Adding / changing event handling

1. If it's a new system `<control>` / `<action>` code, add it to the
   relevant `StrEnum` in `runtime/events.py` (and its `*_EVENT_INFO_TAGS`
   map if it carries `<eventInfo>`). Codes seen on hardware but not in
   the cookbook also get logged to `~/src/pyisyox-undocumented-event-codes.md`
   for UDI.
2. Add routing/parsing in `EventDispatcher` if the frame needs to
   mutate a record.
3. Cover it in `tests/test_runtime/test_events.py` /
   `test_lifecycle.py` — `feed_event_frame` is the seam.

### Adding a node-introspection helper

Derive it from data already on the `Node` (nodedef `cmds.accepts`, the
type triple, `properties`) — **not** a hardcoded type-prefix list.
That keeps it protocol-agnostic and PG3-plugin-friendly. Add fixtures

- assertions in `tests/test_runtime/test_node_introspection.py`.

## Deferred / out of scope

- **Z-Wave parameter & lock-code wire surface** — different endpoints
  (`/rest/(zmatter/)?zwave/node/<addr>/...`), needs a live capture on
  Z-Wave hardware. Tracked as an issue; a `Node.zwave` namespace will
  land once the wire shape is confirmed.
- **Composite climate setpoint (heat+cool with min-gap), retry /
  debounce policy, HA-platform decision trees** — consumer policy, by
  design. They stay in the consumer (e.g. the Home Assistant
  integration), not in pyisyox.

## External Resources

- **GitHub:** <https://github.com/shbatm/pyisyox>
- **PyPI:** <https://pypi.org/project/pyisyox/>
- **Docs:** <https://pyisyox.readthedocs.io>
- **UDI developer resources:** <https://www.universal-devices.com/developers/>
- **ISY994 Developer Cookbook** — §8.5 (WebSocket event structures),
  §8.6 (REST). Pre-IoX-6 but still the authoritative wire reference.
