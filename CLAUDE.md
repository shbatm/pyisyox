# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

**PyISYoX** is a Python library for asynchronous communication with Universal Devices ISY/IoX controllers. It supports:

- ISY994 (legacy hardware family)
- ISY-on-Anything (IoX) hardware: eisy, Polisy
- Protocols: Insteon, X10, Z-Wave, Zigbee/Matter (via supported hardware)

The library enables monitoring and control of nodes, programs, variables, node servers, and networking modules with automatic real-time updates via WebSocket or TCP event streams.

**Lineage**: PyISYoX originated from [PyISY](https://github.com/automicus/PyISY) (by Ryan Kraus & Greg Laabs), rewritten by [@shbatm] based on requirements from the [Home Assistant ISY994 Integration](https://www.home-assistant.io/integrations/isy994/).

**Important**: This project is maintained independently and has no affiliation with Universal Devices, Inc.

## Requirements

- **Minimum Python version**: 3.10
- **Dependencies**: aiohttp, python-dateutil, requests, colorlog, xmltodict

## Development Setup

### Initial Setup

```bash
# Install development dependencies
pip install -r requirements-dev.txt

# Install pre-commit hooks
pre-commit install

# Install library in editable mode
pip install -e .
```

### Testing the Module

```bash
# Quick test - connect and print node summary
python3 -m pyisyox http://polisy.local:8080 admin password

# With all options
python3 -m pyisyox http://your-isy:80 username password --nodes --programs --variables --networking --node-servers
```

### DevContainer Support

A VSCode DevContainer is available for consistent development. On container start, the `examples/` folder is created (not committed to repo) for testing with your ISY connection details.

### Running Tests

```bash
# Run all tests
pytest

# Run with coverage
pytest --cov=pyisyox
```

## Code Quality

### Linting & Formatting

```bash
# Format code with black
black pyisyox

# Sort imports with isort
isort pyisyox

# Run ruff linter
ruff check pyisyox --fix

# Type checking with mypy
mypy pyisyox

# Run pylint
pylint pyisyox
```

### Pre-commit Hooks

- **black**: Code formatting
- **isort**: Import sorting
- **codespell**: Spell checking (ignores ISY-specific terms)
- **yamllint**: YAML validation
- **mypy**: Type checking
- **pylint**: Code quality checks
- **ruff**: Fast Python linter

## Architecture

### Core Components

**ISY Class (`isy.py`)**: Main controller interface

- Entry point for all ISY interactions
- Manages connections, initialization, and shutdown
- Coordinates all platform modules (nodes, programs, variables, etc.)
- Handles event stream setup (WebSocket or legacy TCP)
- Properties: `connected`, `auto_update`, `uuid`, `hostname`
- Methods: `initialize()`, `shutdown()`, `query()`, `send_x10_cmd()`

**Connection Class (`connection.py`)**: HTTP/HTTPS communication

- Manages aiohttp sessions with connection pooling
- Handles authentication, retries, and backoff
- Enforces connection limits:
  - ISY994: 2 HTTPS / 5 HTTP concurrent
  - IoX: 20 HTTPS / 50 HTTP concurrent
- `ISYConnectionInfo`: Dataclass holding URL, auth, WebSocket details
- `test_connection()`: Validates connectivity and retrieves config

**Configuration (`configuration.py`)**: ISY configuration data

- Parses and stores ISY system configuration
- `ConfigurationData`: uuid, name, model, firmware, platform, networking status
- Auto-detects ISY994 vs IoX platform

### Platform Modules

Each platform follows a similar pattern with collection classes and entity classes:

**Nodes (`nodes/`)**: Physical and virtual devices

- `Nodes`: Collection class, manages all nodes and groups
- `Node`: Individual device (Insteon, Z-Wave, etc.)
- `Group`: ISY scene (collection of nodes)
- `NodeBase`: Base class with common node functionality
- `Folder`: Organizational folder structure
- Key properties: `status`, `uom`, `precision`, `formatted`, `parent_node`
- Node families: Insteon, Z-Wave, Zigbee, Node Server, UPB, Brultech, NCD, etc.

**Programs (`programs/`)**: ISY programs (conditions/actions)

- `Programs`: Collection class
- `Program`: Individual program with status and actions
- `ProgramDetail`: Metadata (last_edited, last_run, enabled)
- `Folder`: Program folder structure

**Variables (`variables/`)**: ISY variables (integer/state)

- `Variables`: Collection class with separate integer/state containers
- `Variable`: Individual variable
- Properties: `status` (current value), `initial`, `precision`, `variable_id`

**Node Servers (`node_servers.py`)**: Polyglot node server support

- `NodeServers`: Manages node server definitions
- `NodeDef`: Device definitions from node servers
- Used for custom device types beyond standard Insteon/Z-Wave

**Networking (`networking.py`)**: Network resources

- `NetworkResources`: Collection of network commands
- `NetworkCommand`: Individual network resource (HTTP GET/POST, etc.)

**Clock (`clock.py`)**: ISY system clock/time

- `Clock`: ISY time information
- Properties: `datetime`, `tz_offset`, `is_dst`, `sunrise`, `sunset`

### Event System

**Event Architecture**:

- All entities inherit from `Entity` class with event emission
- Event emitters use `EventEmitter` (pub/sub pattern)
- Event listeners use `EventListener` (subscription handle)
- Events flow: ISY → Event Stream → Router → Entity Updates

**Event Streams** (`events/`):

- `WebSocketClient` (`websocket.py`): Preferred for IoX, auto-reconnect
- `EventStream` (`tcpsocket.py`): Legacy TCP stream for ISY994
- `EventReader` (`eventreader.py`): Parses event XML
- `EventRouter` (`router.py`): Routes events to appropriate handlers using match/case

**Event Types** (`helpers/events.py`):

- `NodeChangedEvent`: Node status/property changes
- `SystemStatus`: ISY system status (BUSY, IDLE, SAFE_MODE, etc.)
- `EventStreamStatus`: Stream connection status

### Helper Modules (`helpers/`)

**Entity System** (`entity.py`, `entity_platform.py`):

- `Entity`: Base class for all ISY objects (nodes, programs, variables)
- `EntityStatus`: Status information wrapper
- Generic type system using TypeVars for status types

**Models** (`models.py`):

- `NodeProperty`: Auxiliary node properties (on_level, ramp_rate, etc.)
- `ZWaveProperties`: Z-Wave specific properties and parameters
- `ZWaveParameter`: Individual Z-Wave parameter

**Session Management** (`session.py`):

- `get_new_client_session()`: Creates aiohttp session with proper config
- `get_sslcontext()`: SSL/TLS context for secure connections
- Handles custom TLS versions if specified

**XML Parsing** (`xml.py`):

- `parse_xml()`: Main XML parser using xmltodict
- Helper functions: `value_from_xml()`, `attr_from_xml()`, `attr_from_element()`

**Events** (`events.py`):

- `EventEmitter`: Publisher class for event notifications
- `EventListener`: Subscriber handle for event subscriptions
- Thread-safe event dispatching

### Constants (`constants.py`)

**Critical constants** (extensive file, 900+ lines):

- **URLs**: REST API endpoints (`URL_NODES`, `URL_PROGRAMS`, `URL_VARIABLES`, etc.)
- **Commands**: Device commands (`CMD_ON`, `CMD_OFF`, `CMD_DIM`, etc.)
- **Properties**: Node properties (`PROP_STATUS`, `PROP_ON_LEVEL`, `PROP_RAMP_RATE`, etc.)
- **UOM**: Units of Measure mappings (temperature, percentage, power, etc.)
- **State Mappings**: `UOM_TO_STATES` for translating numeric values to states
- **Device Types**: Insteon types, Z-Wave categories, node families
- **Protocols**: Insteon, Z-Wave, Zigbee, Node Server, etc.
- **System Status**: BUSY, IDLE, SAFE_MODE, WRITE_TO_EEPROM, etc.

## Usage Patterns

### Basic Connection

```python
from pyisyox import ISY
from pyisyox.connection import ISYConnectionInfo

connection_info = ISYConnectionInfo(
    "http://polisy.local:8080",
    "admin",
    "password"
)

isy = ISY(connection_info, use_websocket=True)

await isy.initialize(
    nodes=True,
    programs=True,
    variables=True,
    networking=False,
    node_servers=False
)
```

### Event Subscription

```python
def node_changed_handler(event, key):
    print(f"Node {event.address} changed: {event.action}")

# Subscribe to node events
listener = isy.nodes.status_events.subscribe(
    node_changed_handler,
    key="my_listener"
)

# Unsubscribe later
listener.unsubscribe()
```

### Controlling Devices

```python
# Turn on a node
await isy.nodes["1A 2B 3C 1"].turn_on()

# Set dimmer level
await isy.nodes["1A 2B 3C 1"].turn_on(val=128)  # 50%

# Run a program
await isy.programs["My Program"].run_then()

# Set a variable
await isy.variables[2][5].set_value(100)  # Type 2, ID 5
```

## Breaking Changes (v3.2.0+)

**Important for upgrading**:

- Minimum Python 3.10 (was 3.9)
- Minimum ISY firmware 4.3
- `_id` → `_address` (use `address` property)
- Properties are read-only: use `update_status()`, not direct assignment
- Module reorganization: helpers moved to subfolders
- Renamed properties: `prec` → `precision`, `type` → `type_`, `init` → `initial`, `vid` → `variable_id`

## Code Style

Follows standard Python conventions with Home Assistant influence:

- **Black** for formatting (target Python 3.10-3.11)
- **isort** with multi_line_output=3, line_length=88
- **Pylint** with Home Assistant config (complexity checks mostly disabled)
- **Ruff** for fast linting (same rules as Home Assistant)
- **Type hints** enforced with mypy
- **Docstrings** required (Google style, D213)

## Documentation

- **ReadTheDocs**: https://pyisyox.readthedocs.io (partial, being updated)
- **Docs source**: `docs/` directory with Sphinx
- **Build docs**: `cd docs && make html`
- **API documentation**: Auto-generated from docstrings

## Testing & Quality Assurance

### Test Configuration

- **Framework**: pytest with asyncio support
- **Test paths**: `tests/`
- **Async mode**: auto
- **Logging**: Custom format with timestamps

### Type Checking

- **mypy** configured with strict settings
- Type stubs included (`py.typed` marker)
- Required for: `python-dateutil`, `PyYAML`

## Integration with hacs-isy994

The `../hacs-isy994` repository uses PyISYoX as its core library:

- Home Assistant custom component wraps PyISYoX
- Test beta PyISYoX features before merging to HA Core
- Co-development supported via DevContainer mounts

When developing both:

1. Mount both repos in DevContainer
2. Install PyISYoX in editable mode: `pip3 install -e /workspaces/PyISY`
3. Test changes in hacs-isy994 immediately

## Common Development Workflows

### Adding a New Node Type

1. Add constants to `constants.py` (type codes, UOM mappings)
2. Update `Node` class in `nodes/node.py` if special handling needed
3. Test with real ISY device or mock data
4. Update hacs-isy994 filters in `const.py` for Home Assistant integration

### Adding Event Handling

1. Define event in `helpers/events.py` or `helpers/models.py`
2. Add routing logic in `events/router.py` (match/case statement)
3. Subscribe to events in consuming code via `EventEmitter`

### Debugging Connection Issues

- Enable verbose logging: `enable_logging(level=LOG_VERBOSE)`
- Check connection limits (ISY994 is strict: max 2 HTTPS connections)
- Use `python3 -m pyisyox` with `--debug` flag
- Monitor ISY admin console for connection errors

## External Resources

- **GitHub**: https://github.com/shbatm/pyisyox
- **PyPI**: https://pypi.org/project/pyisyox/
- **UDI Developer Resources**: https://www.universal-devices.com/developers/
- **ISY REST API**: Documented in ISY admin console
