# PyISYoX Connection Flow

This document provides a comprehensive narrative of how a connection is established when a new instance of PyISYoX is created, including the sequence of REST API endpoint calls and event stream initialization.

## Table of Contents

- [Overview](#overview)
- [Step 1: Initialization](#step-1-initialization)
- [Step 2: Connection Testing](#step-2-connection-testing)
- [Step 3: Platform Initialization](#step-3-platform-initialization)
- [Step 4: Event Stream Setup](#step-4-event-stream-setup)
- [Complete Endpoint Call Sequence](#complete-endpoint-call-sequence)
- [Connection Architecture](#connection-architecture)

## Overview

The PyISYoX connection lifecycle consists of four main phases:

1. **Initialization** - Creating the ISY object and connection infrastructure
2. **Connection Testing** - Validating credentials and fetching ISY configuration
3. **Platform Initialization** - Loading entities (nodes, programs, variables, etc.)
4. **Event Stream Setup** - Establishing real-time updates via WebSocket or TCP

## Step 1: Initialization

### Code Example

```python
from pyisyox import ISY
from pyisyox.connection import ISYConnectionInfo

# Create connection info
connection_info = ISYConnectionInfo(
    "http://polisy.local:8080",
    "admin",
    "password"
)

# Create ISY instance
isy = ISY(connection_info, use_websocket=True)
```

### What Happens

1. **ISYConnectionInfo Creation** (`connection.py:42-63`)
   - Parses the URL and determines if HTTPS is used
   - Creates REST URL: `{url}/rest`
   - Creates WebSocket URL: `{rest_url.replace('http', 'ws')}/subscribe`
   - Stores authentication credentials as `aiohttp.BasicAuth`

2. **ISY Class Instantiation** (`isy.py:62-94`)
   - Creates `Connection` object with connection info
   - Initializes connection semaphore:
     - ISY994: 2 HTTPS / 5 HTTP concurrent connections
     - IoX: 20 HTTPS / 50 HTTP concurrent connections (upgraded later)
   - Creates `aiohttp.ClientSession` with connection pooling
   - Initializes platform classes (executed **before** any network calls):
     - `Clock` - ISY time/location management
     - `NetworkResources` - Network commands
     - `Variables` - ISY variables (integer and state)
     - `Programs` - ISY programs
     - `Nodes` - Devices, groups, and folders
     - `NodeServers` - Polyglot node server definitions
   - Creates `WebSocketClient` (if `use_websocket=True`) or prepares for TCP `EventStream`
   - Initializes event emitters for connection and status events

**Important**: At this point, **no network calls have been made**. The ISY object exists but is not connected.

## Step 2: Connection Testing

### Code Example

```python
# Must be called to actually connect
await isy.initialize()
```

### What Happens

The `initialize()` method begins with connection validation (`isy.py:96-106`):

```python
self.config = await self.conn.test_connection()
```

### REST API Call #1: `/rest/config`

**Purpose**: Validate connection and retrieve ISY system configuration

**Called By**: `Connection.test_connection()` → `Configuration.update()` (`configuration.py:101-146`)

**Response Contains**:

- ISY UUID (unique identifier)
- Firmware version
- Platform type (ISY994 vs IoX)
- Device model and name
- Installed features (Z-Wave, Networking Module, Portal Integration, etc.)
- Module capabilities (variables enabled, node definitions, etc.)

**Configuration Detection**:

```python
if self.config.platform == "IoX":
    self.conn.increase_available_connections()
```

If the ISY is running on IoX hardware (eisy, Polisy), the connection limits are increased:

- From 2 → 20 HTTPS connections
- From 5 → 50 HTTP connections

This allows for faster parallel loading of large systems.

## Step 3: Platform Initialization

After connection validation, platform data is loaded **in parallel** using `asyncio.gather()` (`isy.py:111-129`).

### Parallel Loading Strategy

```python
isy_setup_tasks = []
if nodes:
    isy_setup_tasks.append(self.nodes.initialize())
if clock:
    isy_setup_tasks.append(self.clock.update())
if programs:
    isy_setup_tasks.append(self.programs.update())
if variables:
    isy_setup_tasks.append(self.variables.update())
if networking:
    isy_setup_tasks.append(self.networking.update())

await asyncio.gather(*isy_setup_tasks)
```

Each platform loads concurrently to minimize initialization time. Let's examine each platform's endpoint calls:

---

### 3.1 Nodes Platform

**Method**: `Nodes.initialize()` (`nodes/__init__.py:74-92`)

The Nodes platform requires **two** REST calls with special orchestration:

#### Call #2a: `/rest/status` (Started First)

**Purpose**: Get current status values for all nodes

**Why First**: This endpoint typically takes the longest to download, so it's started as a background task while nodes are being loaded.

**Response Contains**:

- Node addresses
- Current status values for all properties
- Unit of measurement (UOM) codes
- Precision/formatting information

```python
status_task = asyncio.create_task(self.update_status())
```

#### Call #2b: `/rest/nodes` (Simultaneous)

**Purpose**: Get node definitions and metadata

**Response Contains**:

- Folders (organizational hierarchy)
- Nodes (individual devices):
  - Address, name, type
  - Device category (Insteon type, Z-Wave category, etc.)
  - Node definition ID (for custom node servers)
  - Parent/child relationships
  - Device family (Insteon, Z-Wave, Zigbee, Node Server, etc.)
  - Enabled/disabled state
- Groups (ISY scenes/controllers)

```python
nodes_task = asyncio.create_task(self.update())
```

**Node Parsing Flow**:

1. Parse folders first (organizational structure)
2. Parse individual nodes (devices)
3. Parse groups (scenes)
4. Once both tasks complete, merge status data into node objects

**Result**: All nodes are loaded with their current status, properties, and metadata.

---

### 3.2 Programs Platform

**Method**: `Programs.update()` (`programs/__init__.py:60-86`)

#### Call #3: `/rest/programs?subfolders=true`

**Purpose**: Get all ISY programs with folder hierarchy

**Query Parameter**: `subfolders=true` ensures nested folders are included

**Response Contains**:

- Program folders (organizational hierarchy)
- Programs:
  - ID, name, parent folder
  - Status (condition: true/false)
  - Enabled/disabled state
  - Run at startup flag
  - Last run time
  - Last finish time
  - Running status (idle, running then, running else)

**Parsing**: Programs are differentiated from folders using the `folder` tag in the XML response.

---

### 3.3 Variables Platform

**Method**: `Variables.update()` (`variables/__init__.py:53-77`)

The Variables platform makes **four parallel requests** to fetch both definitions and current values:

#### Call #4a: `/rest/vars/definitions/1`

**Purpose**: Get integer variable definitions (names, IDs, precision)

#### Call #4b: `/rest/vars/definitions/2`

**Purpose**: Get state variable definitions (names, IDs, precision)

#### Call #4c: `/rest/vars/get/1`

**Purpose**: Get current values for all integer variables

#### Call #4d: `/rest/vars/get/2`

**Purpose**: Get current values for all state variables

```python
endpoints = [
    [URL_VARIABLES, URL_DEFINITIONS, VAR_INTEGER],  # /rest/vars/definitions/1
    [URL_VARIABLES, URL_DEFINITIONS, VAR_STATE],    # /rest/vars/definitions/2
    [URL_VARIABLES, URL_GET, VAR_INTEGER],          # /rest/vars/get/1
    [URL_VARIABLES, URL_GET, VAR_STATE],            # /rest/vars/get/2
]
```

**Why Separate Calls**: ISY distinguishes between:

- **Integer variables** (type 1): General-purpose integer storage
- **State variables** (type 2): Program state storage

**Variable Parsing**:

1. Check if variables are enabled in config
2. Parse definitions (names, IDs, precision/scale)
3. Parse current values (initial value, current value)
4. Merge definitions with values to create Variable objects

**Edge Cases Handled**:

- No variables defined: returns empty
- Single variable: response is a dict instead of list
- Variables disabled in ISY config

---

### 3.4 Networking Platform

**Method**: `NetworkResources.update()` (`networking.py:48-71`)

#### Call #5: `/rest/networking/resources`

**Purpose**: Get network resource commands

**Conditional**: Only called if `config.networking` or `config.portal` is enabled

**Response Contains**:

- Network resource IDs and names
- Command type (GET, POST, etc.)
- URL or IP address
- Authentication details
- Enabled/disabled state

**Note**: Network resources are typically used for:

- HTTP GET/POST commands
- Integration with external systems
- Custom automation triggers

---

### 3.5 Clock Platform

**Method**: `Clock.update()` (`clock.py`)

#### Call #6: `/rest/time`

**Purpose**: Get ISY clock/location information

**Response Contains**:

- Current date/time (NTP timestamp)
- Time zone offset
- DST (Daylight Saving Time) status
- Latitude and longitude
- Sunrise time (calculated)
- Sunset time (calculated)
- Military time format preference

**Special Handling**: ISY uses NTP timestamps with a custom EPOCH offset (36524 days). PyISYoX converts these to Python `datetime` objects.

---

### 3.6 Node Servers Platform (Optional)

**Method**: `NodeServers.update()` (`node_servers.py`)

#### Call #7: `/rest/profiles/ns` (Optional)

**Purpose**: Get Polyglot node server profile definitions

**Conditional**: Only called if `node_servers=True` in `initialize()`

**Response Contains**:

- Node server slot information
- Node type definitions (custom device types)
- Command definitions
- Status definitions
- Editor definitions (for admin console)

**Use Case**: Required for custom node servers (Polyglot plugins) that define non-standard device types beyond Insteon/Z-Wave.

---

## Step 4: Event Stream Setup

After all platforms are initialized, real-time event updates can be enabled.

### WebSocket Event Stream (Default for IoX)

**Method**: `WebSocketClient.start()` (`events/websocket.py:68-74`)

#### WebSocket Connection: `ws://{host}/rest/subscribe`

**Protocol Headers**:

```python
{
    "Sec-WebSocket-Protocol": "ISYSUB",
    "Sec-WebSocket-Version": "13",
    "Origin": "com.universal-devices.websockets.isy"
}
```

**Connection Process**:

1. Establish WebSocket connection with ISY
2. Authenticate using same credentials as REST API
3. Receive stream ID from ISY
4. Begin receiving real-time events as XML messages

**Heartbeat Monitoring**:

- ISY sends heartbeat every 30 seconds
- If heartbeat missed by 35 seconds (30 + 5 grace), connection is reset
- Auto-reconnect with exponential backoff: 0.01s, 1s, 10s, 30s, 60s

**Event Types Received**:

- Node status changes
- Program status changes
- Variable value changes
- System status (BUSY, IDLE, SAFE_MODE, etc.)
- Trigger events (DON, DOF, etc.)

### TCP Event Stream (Legacy, ISY994)

**Method**: `EventStream` (`events/tcpsocket.py`)

**Connection**: Raw TCP socket to ISY on HTTP port

If WebSocket is disabled (`use_websocket=False`):

```python
isy.auto_update = True  # Starts TCP event stream
```

**Process**:

1. Opens TCP connection to ISY
2. Sends subscription request
3. Receives chunked XML event data
4. Manually parses XML messages (more complex than WebSocket)

**Note**: WebSocket is preferred and enabled by default. TCP is only used for older ISY994 firmware that doesn't support WebSockets.

---

### Event Routing

All events (WebSocket or TCP) are processed by `EventRouter` (`events/router.py`):

```python
class EventRouter:
    def process_event(self, event_data):
        match event.action:
            case "_0":  # Heartbeat
                self.websocket.heartbeat()
            case "_1":  # Node changed
                isy.nodes.update_received(event)
            case "_2":  # Variable changed
                isy.variables.update_received(event)
            case "_3":  # Program changed
                isy.programs.update_received(event)
            # ... etc
```

Events are routed to the appropriate platform's `update_received()` method, which updates entity state and fires event notifications.

---

## Complete Endpoint Call Sequence

Here is the **complete order** of REST API calls when initializing PyISYoX with all options enabled:

### Phase 1: Connection Validation (Sequential)

1. **`GET /rest/config`** - Validate connection and get system configuration

### Phase 2: Platform Data Loading (Parallel)

2. **`GET /rest/status`** - Get all node status values (started first, longest)
3. **`GET /rest/nodes`** - Get all node definitions
4. **`GET /rest/programs?subfolders=true`** - Get all programs
5. **`GET /rest/vars/definitions/1`** - Get integer variable definitions
6. **`GET /rest/vars/definitions/2`** - Get state variable definitions
7. **`GET /rest/vars/get/1`** - Get integer variable values
8. **`GET /rest/vars/get/2`** - Get state variable values
9. **`GET /rest/networking/resources`** - Get network resources (if enabled)
10. **`GET /rest/time`** - Get clock/location info
11. **`GET /rest/profiles/ns`** - Get node server definitions (if enabled)

### Phase 3: Real-Time Event Stream (Post-Initialization)

12. **WebSocket** `ws://{host}/rest/subscribe` - Establish event stream

### Total Initial Load

- **Minimum**: 1 config + 2 platform calls = **3 requests** (minimal setup)
- **Typical**: 1 config + 10 platform calls = **11 requests** (full setup)
- **Maximum**: 1 config + 11 platform calls = **12 requests** (with node servers)

**Performance**: All platform calls execute in parallel via `asyncio.gather()`, limited only by the connection semaphore (2-20 concurrent connections depending on platform).

---

## Connection Architecture

### Connection Pooling

PyISYoX uses `aiohttp.ClientSession` with connection pooling for efficiency:

```python
session = aiohttp.ClientSession(
    connector=aiohttp.TCPConnector(
        limit=MAX_CONNECTIONS,  # 2 or 20 for HTTPS
        limit_per_host=MAX_CONNECTIONS
    )
)
```

**Benefits**:

- Reuses TCP connections across requests
- Reduces handshake overhead
- Improves performance for parallel loading

### Request Retry Logic

Every REST request includes automatic retry with exponential backoff (`connection.py:128-208`):

**Retry Strategy**:

```python
MAX_RETRIES = 5
RETRY_BACKOFF = [0.01, 0.10, 0.25, 1, 2]  # Seconds
```

**Retry Conditions**:

- `503 Service Unavailable` - ISY too busy
- Timeout errors (30s default)
- Network errors (connection reset, disconnected)

**Non-Retry Conditions**:

- `401 Unauthorized` - Invalid credentials (raises exception immediately)
- `404 Not Found` - Invalid endpoint (returns None or "" if `ok404=True`)

### Semaphore-Based Rate Limiting

To prevent overwhelming the ISY, all requests are controlled by an `asyncio.Semaphore`:

```python
async with self.semaphore:
    async with self.req_session.get(url, ...) as response:
        # Process response
```

**Limits**:

- **ISY994 HTTPS**: 2 concurrent connections
- **ISY994 HTTP**: 5 concurrent connections
- **IoX HTTPS**: 20 concurrent connections
- **IoX HTTP**: 50 concurrent connections

This ensures PyISYoX never exceeds the ISY's connection limits, which would cause request failures.

---

## Connection State Machine

PyISYoX tracks connection state through event notifications:

```python
class EventStreamStatus(StrEnum):
    NOT_STARTED = "not_started"
    INITIALIZING = "stream_initializing"
    LOADED = "stream_loaded"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    LOST_CONNECTION = "lost_connection"
    RECONNECTING = "reconnecting"
    STOP_UPDATES = "stop_updates"
```

**State Transitions**:

1. `NOT_STARTED` - Initial state
2. `INITIALIZING` - WebSocket connecting
3. `LOADED` - First heartbeat received
4. `CONNECTED` - Fully connected and receiving events
5. `LOST_CONNECTION` - Connection dropped (auto-reconnects)
6. `RECONNECTING` - Attempting to reconnect
7. `DISCONNECTED` - Cleanly disconnected
8. `STOP_UPDATES` - Event stream stopped by user

---

## Summary

The PyISYoX connection flow is designed for:

- **Reliability**: Automatic retries, connection pooling, heartbeat monitoring
- **Performance**: Parallel loading, connection semaphores, optimal request ordering
- **Flexibility**: Optional platform loading, WebSocket or TCP events
- **Real-time**: Event-driven updates via WebSocket with auto-reconnect

By understanding this flow, developers can:

- Optimize initialization for specific use cases
- Debug connection issues effectively
- Extend PyISYoX with new platforms or features
- Integrate PyISYoX into larger applications (like Home Assistant)

For more information, see:

- [PyISYoX Documentation](https://pyisyox.readthedocs.io)
- [ISY REST API Documentation](https://www.universal-devices.com/developers/)
- [Home Assistant ISY994 Integration](https://www.home-assistant.io/integrations/isy994/)
