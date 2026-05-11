# Connection Flow

This document describes how PyISYoX establishes and maintains a
connection to an eisy / Polisy controller running IoX 6.0.0+. It
covers the wire-level endpoint sequence, the parallel load
orchestration, authentication, and the WebSocket reader's state
machine.

If you only want to _use_ the library, start with the
[quickstart](quickstart.rst); this document is for understanding what
`Controller.connect()` does under the hood, debugging connection
issues, or porting the protocol to another language.

## Architectural layers

```
┌────────────────────────────────────────────────────────────┐
│           pyisyox.Controller (controller.py)               │
│  glue: lifecycle, listener registration, helper mutations  │
└──────┬──────────────────────────────────────┬──────────────┘
       │                                      │
       ▼                                      ▼
┌──────────────────────┐               ┌──────────────────────────┐
│ IoXClient (client.py)│               │ WebSocketEventStream     │
│  parallel load fan-  │               │   (runtime/ws.py)        │
│  out, JSON-first;    │               │  read loop + reconnect   │
│  retries on 401      │               │  + status listeners      │
└──────┬──────────┬────┘               └─────────────┬────────────┘
       │          │                                   │
       ▼          ▼                                   ▼
┌──────────┐ ┌───────────┐                ┌──────────────────────┐
│ Auth     │ │ Profile   │                │ EventDispatcher      │
│ (auth.py)│ │ (schema/) │                │   (runtime/events.py)│
│ Portal / │ │ nodedefs, │                │  parses frames,      │
│ LocalAuth│ │ editors   │                │  applies to records, │
│          │ │           │                │  fires listeners     │
└──────────┘ └───────────┘                └──────────────────────┘
```

The layers are deliberately decoupled. `IoXClient` is auth-mode-
agnostic — it takes any `Auth` implementation. `EventDispatcher` is
transport-agnostic — `WebSocketEventStream` is one feeder; tests inject
synthetic frames directly via `Controller.feed_event_frame`.

## Picking an auth mode

The eisy exposes two listeners with different feature sets:

| Mode           | Port    | Credentials             | Wire auth                 | Surface                                          |
| -------------- | ------- | ----------------------- | ------------------------- | ------------------------------------------------ |
| **PortalAuth** | `:443`  | Portal email + password | JWT bearer (auto-refresh) | `/api/*` (JSON) + `/rest/*`                      |
| **LocalAuth**  | `:8443` | Local admin user + pass | HTTP basic                | `/rest/*` only, plus a feature-degraded `/api/*` |

**PortalAuth is the recommended default.** It unlocks the modern JSON
`/api/triggers` AST and `/api/variables` (with names + timestamps), and
the eisy validates the credentials and signs the JWT locally — no
my.isy.io round-trip during steady-state operation. Verified
offline-safe on 2026-05-07.

**LocalAuth exists for users who refuse to use a portal account.** It
has no login round-trip (basic on every request) but cannot read the
modern JSON endpoints; PyISYoX falls back to legacy XML where needed.

The smoke-test CLI picks the mode based on the username (anything
with `@` is treated as a portal email):

```bash
python3 -m pyisyox https://eisy.local:443  me@example.com portal-pw
python3 -m pyisyox https://eisy.local:8443 admin           admin-pw
```

### PortalAuth lifecycle

1. **Login** — `POST /api/login` body `{"username": "<email>",
"password": "<password>"}`. Response is `{"successful": true,
"data": {"accessToken": "<es256-jwt>", "refreshToken": "<es256-jwt>",
"ssl": {…}, …}}`. The library decodes the JWTs' `exp` claims for
   proactive refresh scheduling.

   > **Security note**: the login response leaks the PG3 MQTT TLS
   > keypair under `data.ssl`. PyISYoX's `redact_sensitive()` helper
   > scrubs it before any debug logging.

2. **Per-request** — every HTTP call carries
   `Authorization: Bearer <accessToken>`. If the token expires within
   the next 60 seconds (`PROACTIVE_REFRESH_LEEWAY`), the client
   refreshes _before_ the request to avoid the cost of an in-flight
   401 + refresh + retry.

3. **Refresh** — `POST /api/jwt/refresh` body `{"refreshToken": "<rt>"}`,
   same response shape as login.

4. **401 recovery** — on a 401, the client asks the auth strategy to
   recover (`handle_unauthorized`). PortalAuth tries refresh; if that
   fails, it falls back to a full login. Concurrent 401s collapse onto
   a single refresh round-trip via an internal lock.

5. **Logout** — `Controller.stop()` best-effort posts
   `POST /api/logout` to invalidate the server-side session. Any
   error is logged at debug level and swallowed; the long-lived
   refresh token will expire naturally on its TTL.

### LocalAuth lifecycle

No login round-trip — every request carries `Authorization: Basic …`
attached via `aiohttp.BasicAuth`. A 401 means the credentials are
wrong, so re-auth cannot recover and the client raises
`AuthError` to the caller.

## The connect() call

`await controller.connect()` runs three phases:

### Phase 1 — Config

```
GET /api/config        →  {"data": {"uuid": "...", "version": "6.0.0", "portalHost": "..."}}
```

Cheap, unauthenticated. Confirms the controller is reachable and is
running an IoX 6+ firmware. The returned `ControllerConfig` is
attached to the `LoadResult`.

### Phase 2 — Authenticate

`auth.authenticate(session, base_url)` runs exactly once across
concurrent first-use callers (lock-then-recheck inside `IoXClient`).

- **LocalAuth** — no-op.
- **PortalAuth** — `POST /api/login`, stores the `TokenPair`.

### Phase 3 — Parallel load fan-out

`asyncio.gather(...)` fires the next nine requests in parallel:

| #   | Endpoint                                               | Shape | Used for                                                                                                                                     |
| --- | ------------------------------------------------------ | ----- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| 1   | `GET /rest/profiles?include=nodedefs,editors,linkdefs` | JSON  | The `Profile` blob — every nodedef + editor + linkdef. ~117 KB.                                                                              |
| 2   | `GET /api/nodes`                                       | JSON  | Node structure (family/instance, addresses, parent/pnode, flags). Plugin nodes have no `property[]` field.                                   |
| 3   | `GET /rest/nodes`                                      | XML   | Group / folder structure (`<group>`, `<folder>` elements).                                                                                   |
| 4   | `GET /rest/status`                                     | XML   | Canonical property table for every node — including plugin nodes. Merged into the JSON node records to fill the missing `property[]` fields. |
| 5   | `GET /api/programs`                                    | JSON  | Programs and program-folders (one flat list, discriminated by `is_folder`).                                                                  |
| 6   | `GET /api/triggers`                                    | JSON  | Program AST. Stays raw for consumers.                                                                                                        |
| 7   | `GET /api/variables/1`                                 | JSON  | Integer variables with names + timestamps.                                                                                                   |
| 8   | `GET /api/variables/2`                                 | JSON  | State variables.                                                                                                                             |
| 9   | `GET /rest/networking/resources` (optional)            | XML   | Network resources, if the module is enabled. A 404 / 503 here is tolerated — the load doesn't abort if the module isn't installed.           |

Total: **8–9 HTTP requests in parallel**, plus the config call from
Phase 1, plus (if requested) the WebSocket upgrade.

After the gather:

- `Profile.load_from_json(profile_raw)` parses the family/instance
  tree and builds the `(nodedef_id, family_id, instance_id) → NodeDef`
  lookup.
- The `/rest/status` overlay is merged into the `/api/nodes` records
  via `merge_status_into_nodes`. Native nodes get any missing
  properties filled in; plugin nodes (which carry no `property[]` in
  the JSON) get _all_ their properties from the overlay.
- Programs, variables, network resources, and groups/folders are
  parsed into their record types.

The result is one `LoadResult` dataclass with all the data the
runtime wrappers need. Each wrapper holds a _reference_ to its record,
so WebSocket frames that mutate the record in place are visible to the
wrapper without an explicit notification path.

### 401 recovery during load

`IoXClient._get_text` retries the request once on 401 if
`auth.handle_unauthorized` returns `True`. After the retry attempt is
spent (or recovery returns `False`), the next 401 raises `AuthError`.

Any non-2xx (after the optional retry) raises `HTTPError` with the
status and URL — the parallel `asyncio.gather` propagates the first
exception.

## WebSocket upgrade

`Controller.connect(start_websocket=True)` (the default) constructs a
`WebSocketEventStream` and calls `start()`, which schedules a
background `asyncio.Task`.

### Connect handshake

1. Build the WS URL by rewriting the base URL's scheme (`https://` →
   `wss://`) and appending the configured path (default
   `/rest/subscribe`; the modern JSON-envelope path
   `/api/events/subscribe` is opt-in).
2. Pull `auth.request_kwargs(session, base_url)` and pass them to
   `session.ws_connect(...)`. `LocalAuth` returns
   `{"auth": aiohttp.BasicAuth(...)}` — aiohttp's `ws_connect` accepts
   `auth` directly. `PortalAuth` returns
   `{"headers": {"Authorization": "Bearer …"}}`, passed through
   verbatim.
3. On success, transition to `EventStreamStatus.CONNECTED` and notify
   any status listeners.

### Read loop

```python
async for msg in ws:
    if msg.type == aiohttp.WSMsgType.TEXT:
        event = dispatcher.feed(msg.data)
        last_event_at = now()
    elif msg.type in (CLOSE, ERROR):
        break
```

Each `dispatcher.feed(raw)`:

1. Parses the frame (XML or JSON-envelope; non-event JSON frames like
   PG3 `spolisy` are ignored).
2. Applies the update to the underlying record in `LoadResult` —
   property dict, program status, or variable record — _before_
   firing listeners.
3. Emits the parsed `Event` (and, if applicable, a
   `NodeLifecycleEvent` or `ProgramStatusEvent`) to subscribers.

See [events.rst](events.rst) for the full event taxonomy.

### Reconnection

On transport error or unexpected close, the reader backs off through
a fixed schedule and tries again:

```
backoff: 1s → 2s → 5s → 10s → 30s → 60s   (capped at 60s thereafter)
```

The schedule resets after a successful read. Status listeners see
`EventStreamStatus.RECONNECTING` while we're in the backoff loop and
`EventStreamStatus.CONNECTED` once a fresh handshake succeeds.

A 401-class WebSocket handshake failure triggers `auth.handle_unauthorized`
to refresh tokens before the next reconnect attempt — PortalAuth
recovers in the background; LocalAuth's `handle_unauthorized` returns
`False`, which lets the next attempt's basic-auth header carry the
(possibly updated) credentials.

### WebSocket health surface

For consumers wanting to surface stream health to the user (HA's
system_health card, diagnostics dumps, etc.), the live stream is
exposed via `Controller.websocket`:

```python
ws = controller.websocket
if ws is not None:
    print(ws.status)            # CONNECTING / CONNECTED / RECONNECTING / DISCONNECTED
    print(ws.connected)         # bool
    print(ws.last_event_at)     # datetime in UTC, or None
```

`Controller.websocket` is `None` for one-shot reads
(`connect(start_websocket=False)`) and after `stop()`.

## Refresh and dynamic profile reload

Two methods on `Controller` let consumers absorb controller-side
changes without re-authenticating:

- **`refresh()`** — re-runs Phase 3 (the parallel load fan-out) and
  merges the fresh data into the live `LoadResult`. The dispatcher's
  binding to `LoadResult.nodes` survives because the dict is mutated
  in place. Call this after a `NodeLifecycleEvent.requires_reload`
  signal.

- **`refresh_profile()`** — re-fetches just `/rest/profiles` and
  merges the result into the live `Profile`. Designed for PG3 dynamic
  profile reload (a plugin updates its nodedefs at runtime). Returns
  a `ProfileMergeResult` listing the added vs replaced nodedef keys
  so consumers can re-classify or invalidate caches.

## Shutdown

`await controller.stop()` is symmetric and idempotent:

1. Stop the WebSocket reader (cancel the task, close the WS).
2. `auth.close(session, base_url)` — PortalAuth posts `/api/logout`;
   LocalAuth no-ops.
3. Close the aiohttp session if the controller owns it (when
   `session=None` was passed to the constructor). Sessions injected
   by the consumer are not closed.
4. Drop the loaded snapshot so any post-stop accessor raises
   `ControllerNotConnectedError` instead of returning stale data.

Errors during the auth.close step are swallowed at debug level —
shutdown should never raise.

## TLS

The eisy ships with a self-signed certificate. `Controller`'s default
`verify_ssl=False` accepts it; pass `verify_ssl=True` if the user has
installed their own CA. Pass `tls_version=1.2` or `tls_version=1.3` to
pin the negotiated version (default: auto-negotiate; TLS 1.0 / 1.1 are
rejected by the eisy regardless).

The `tls_version` and `verify_ssl` parameters apply only when the
controller creates its own `aiohttp.ClientSession`. Consumers that
inject their own session are responsible for configuring SSL on it.
