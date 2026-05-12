"""Constants for the PyISYoX Module."""

from __future__ import annotations

import datetime
from enum import IntEnum, IntFlag, StrEnum

# Time Constants / Strings
EMPTY_TIME = datetime.datetime(year=1, month=1, day=1)
MILITARY_TIME = "%Y/%m/%d %H:%M:%S"
STANDARD_TIME = "%Y/%m/%d %I:%M:%S %p"
XML_STRPTIME = "%Y%m%d %H:%M:%S"
XML_STRPTIME_YY = "%y%m%d %H:%M:%S"


class EventStreamStatus(StrEnum):
    """Event Stream Status Codes."""

    LOST_CONNECTION = "lost_connection"
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    START_UPDATES = "start_updates"
    STOP_UPDATES = "stop_updates"
    INITIALIZING = "stream_initializing"
    LOADED = "stream_loaded"
    RECONNECT_FAILED = "reconnect_failed"
    RECONNECTING = "reconnecting"
    DISCONNECTING = "stream_disconnecting"
    NOT_STARTED = "not_started"


ISY_VALUE_UNKNOWN = -1 * float("inf")

X10_COMMANDS: dict[str, int] = {
    "all_off": 1,
    "all_on": 4,
    "on": 3,
    "off": 11,
    "bright": 7,
    "dim": 15,
}

#: ``<enabled>`` element in profile / program XML.
TAG_ENABLED = "enabled"


class Protocol(StrEnum):
    """Entity protocol string enum."""

    FOLDER = "program_folder"
    GROUP = "group"
    INSTEON = "insteon"
    INT_VAR = "integer_variable"
    ISY = "isy"
    NETWORK = "network"
    NODE_SERVER = "node_server"
    NODE_FOLDER = "node_folder"
    PROGRAM = "program"
    STATE_VAR = "state_variable"
    UPB = "upb"
    MATTER = "matter"
    ZIGBEE = "zigbee"
    ZWAVE = "zwave"
    #: Family id present but not one we map to a known device protocol
    #: (RCS, Brultech, NCD, UDI, group families, folders, …).
    UNKNOWN = "unknown"


class NodeFamily(StrEnum):
    """Node family string enum.

    IDs 0-9 come from ``ISY-WSDK-5.0.4/WSDL/family.xsd`` (still the
    latest published WSDL). 10 (Node Server / PG3) and 12-15 are IoX 6
    additions confirmed against eisy hardware: 12 is the Z-Matter
    radio acting as a Z-Wave controller, 15 the same radio acting as
    a Matter/Thread controller, 13 the folder family.
    """

    CORE = "0"
    INSTEON = "1"
    UPB = "2"
    RCS = "3"
    ZWAVE = "4"
    AUTO = "5"
    GENERIC = "6"
    UDI = "7"
    BRULTECH = "8"
    NCD = "9"
    NODESERVER = "10"
    ZMATTER_ZWAVE = "12"
    FOLDER = "13"
    MATTER = "15"


PROP_BATTERY_LEVEL = "BATLVL"
PROP_BUSY = "BUSY"
PROP_COMMS_ERROR = "ERR"
PROP_ENERGY_MODE = "CLIEMD"
PROP_HEAT_COOL_STATE = "CLIHCS"
PROP_HUMIDITY = "CLIHUM"
PROP_ON_LEVEL = "OL"
PROP_RAMP_RATE = "RR"
PROP_SCHEDULE_MODE = "CLISMD"
PROP_SETPOINT_COOL = "CLISPC"
PROP_SETPOINT_COOL_DELTA = "CLISPCD"  # auto-changeover cool setpoint delta
PROP_SETPOINT_HEAT = "CLISPH"
PROP_SETPOINT_HEAT_DELTA = "CLISPHD"  # auto-changeover heat setpoint delta
PROP_STATUS = "ST"
PROP_TEMPERATURE = "CLITEMP"
PROP_UOM = "UOM"
PROP_ZWAVE_PREFIX = "ZW_"

VAR_INTEGER = "1"
VAR_STATE = "2"

CLIMATE_SETPOINT_MIN_GAP = 2

CMD_BACKLIGHT = "BL"
CMD_BEEP = "BEEP"
CMD_BRIGHTEN = "BRT"
CMD_CLIMATE_FAN_SETTING = "CLIFS"
CMD_CLIMATE_MODE = "CLIMD"
CMD_DIM = "DIM"
CMD_DISABLE = "disable"
CMD_DISABLE_RUN_AT_STARTUP = "disableRunAtStartup"
CMD_ENABLE = "enable"
CMD_ENABLE_RUN_AT_STARTUP = "enableRunAtStartup"
CMD_FADE_DOWN = "FDDOWN"
CMD_FADE_STOP = "FDSTOP"
CMD_FADE_UP = "FDUP"
CMD_MANUAL_DIM_BEGIN = "BMAN"  # Depreciated, use Fade
CMD_MANUAL_DIM_STOP = "SMAN"  # Depreciated, use Fade
CMD_MODE = "MODE"
CMD_OFF = "DOF"
CMD_OFF_FAST = "DFOF"
CMD_ON = "DON"
CMD_ON_FAST = "DFON"
CMD_QUERY = "QUERY"
CMD_RESET = "RESET"
CMD_SECURE = "SECMD"
CMD_X10 = "X10"
# Alarm-panel control verbs (Z-Wave / plugin alarm nodedefs).
CMD_ALARM_ARM = "ARM"
CMD_ALARM_DISARM = "DISARM"

COMMAND_FRIENDLY_NAME: dict[str, str] = {
    "ADRPST": "auto_dr_processing_state",
    "AIRFLOW": "air_flow",
    "ALARM": "alarm",
    "ANGLE": "angle_position",
    "ANGLPOS": "angle_position",
    "ATMPRES": "atmospheric_pressure",
    "AWAKE": "awake",
    "BARPRES": "barometric_pressure",
    "CC": "current",
    "CLIFRS": "fan_running_state",
    "CLIFSO": "fan_setting_override",
    "CO2LVL": "co2_level",
    "CPW": "power",
    "CTL": "controller_action",
    "CV": "voltage",
    "DELAY": "delay",
    "DEWPT": "dew_point",
    "DISTANC": "distance",
    "DOF3": "off_3x_key_presses",
    "DOF4": "off_4x_key_presses",
    "DOF5": "off_5x_key_presses",
    "DON3": "on_3x_key_presses",
    "DON4": "on_4x_key_presses",
    "DON5": "on_5x_key_presses",
    "ELECCON": "electrical_conductivity",
    "ELECRES": "electrical_resistivity",
    PROP_COMMS_ERROR: "device_communication_errors",
    "ETO": "evapotranspiration",
    "FATM": "fat_mass",
    "FREQ": "frequency",
    "GPV": "general_purpose",
    "GUST": "gust",
    "GV0": "custom_control_0",
    "GV1": "custom_control_1",
    "GV2": "custom_control_2",
    "GV3": "custom_control_3",
    "GV4": "custom_control_4",
    "GV5": "custom_control_5",
    "GV6": "custom_control_6",
    "GV7": "custom_control_7",
    "GV8": "custom_control_8",
    "GV9": "custom_control_9",
    "GV10": "custom_control_10",
    "GV11": "custom_control_11",
    "GV12": "custom_control_12",
    "GV13": "custom_control_13",
    "GV14": "custom_control_14",
    "GV15": "custom_control_15",
    "GV16": "custom_control_16",
    "GV17": "custom_control_17",
    "GV18": "custom_control_18",
    "GV19": "custom_control_19",
    "GV20": "custom_control_20",
    "GV21": "custom_control_21",
    "GV22": "custom_control_22",
    "GV23": "custom_control_23",
    "GV24": "custom_control_24",
    "GV25": "custom_control_25",
    "GV26": "custom_control_26",
    "GV27": "custom_control_27",
    "GV28": "custom_control_28",
    "GV29": "custom_control_29",
    "GV30": "custom_control_30",
    "GVOL": "gas_volume",
    "HAIL": "hail",
    "HEATIX": "heat_index",
    "HR": "heart_rate",
    "LUMIN": "luminance",
    "METHANE": "methane_density",
    "MOIST": "moisture",
    "MOON": "moon_phase",
    "MUSCLEM": "muscle_mass",
    "OZONE": "ozone",
    "PCNT": "pulse_count",
    "PF": "power_factor",
    "PM10": "particulate_matter_10",
    "PM25": "particulate_matter_2.5",
    "POP": "percent_chance_of_precipitation",
    "PPW": "polarized_power",
    "PRECIP": "precipitation",
    "PULSCNT": "pulse_count",
    "RADON": "radon_concentration",
    "RAINRT": "rain_rate",
    "RELMOD": "relative_modulation_level",
    "RESPR": "respiratory_rate",
    "RFSS": "rf_signal_strength",
    "ROTATE": "rotation",
    "RR": "ramp_rate",
    "SEISINT": "seismic_intensity",
    "SEISMAG": "seismic_magnitude",
    "SMOKED": "smoke_density",
    "SOILH": "soil_humidity",
    "SOILR": "soil_reactivity",
    "SOILS": "soil_salinity",
    "SOILT": "soil_temperature",
    "SOLRAD": "solar_radiation",
    "SPEED": "speed",
    "SVOL": "sound_volume",
    "TANKCAP": "tank_capacity",
    "TEMPEXH": "exhaust_temperature",
    "TEMPOUT": "outside_temperature",
    "TIDELVL": "tide_level",
    "TIME": "time",
    "TIMEREM": "time_remaining",
    "TPW": "total_energy_used",
    "UAC": "user_number",
    "USRNUM": "user_number",
    "UV": "uv_light",
    "VOCLVL": "voc_level",
    "WATERF": "water_flow",
    "WATERP": "water_pressure",
    "WATERT": "water_temperature",
    "WATERTB": "boiler_water_temperature",
    "WATERTD": "domestic_hot_water_temperature",
    "WEIGHT": "weight",
    "WINDCH": "wind_chill",
    "WINDDIR": "wind_direction",
    "WVOL": "water_volume",
    CMD_BACKLIGHT: "backlight",
    CMD_BEEP: "beep",
    CMD_BRIGHTEN: "bright",
    CMD_CLIMATE_FAN_SETTING: "fan_state",
    CMD_CLIMATE_MODE: "climate_mode",
    CMD_DIM: "dim",
    CMD_FADE_DOWN: "fade_down",
    CMD_FADE_STOP: "fade_stop",
    CMD_FADE_UP: "fade_up",
    CMD_MANUAL_DIM_BEGIN: "brighten_manual",
    CMD_MANUAL_DIM_STOP: "stop_manual",
    CMD_MODE: "mode",
    CMD_OFF: "off",
    CMD_OFF_FAST: "fastoff",
    CMD_ON: "on",
    CMD_ON_FAST: "faston",
    CMD_RESET: "reset",
    CMD_SECURE: "secure",
    CMD_X10: "x10_command",
    PROP_BATTERY_LEVEL: "battery_level",
    PROP_BUSY: "busy",
    PROP_ENERGY_MODE: "energy_saving_mode",
    PROP_HEAT_COOL_STATE: "heat_cool_state",
    PROP_HUMIDITY: "humidity",
    PROP_ON_LEVEL: "on_level",
    PROP_SCHEDULE_MODE: "schedule_mode",
    PROP_SETPOINT_COOL: "cool_setpoint",
    PROP_SETPOINT_HEAT: "heat_setpoint",
    PROP_STATUS: "status",
    PROP_TEMPERATURE: "temperature",
    PROP_UOM: "unit_of_measure",
}

#: Control codes that represent a silent property/state update rather than
#: a surfaced device command. Reference helper for consumers deciding which
#: event frames to surface. (Kept pending issue #1.)
EVENT_PROPS_IGNORED: list[str] = [
    CMD_BEEP,
    CMD_BRIGHTEN,
    CMD_DIM,
    CMD_MANUAL_DIM_BEGIN,
    CMD_MANUAL_DIM_STOP,
    CMD_FADE_UP,
    CMD_FADE_DOWN,
    CMD_FADE_STOP,
    CMD_OFF,
    CMD_OFF_FAST,
    CMD_ON,
    CMD_ON_FAST,
    CMD_RESET,
    CMD_X10,
    PROP_BUSY,
]

# Special Units of Measure
UOM_ISYV4_DEGREES = "degrees"
UOM_ISYV4_NONE = "n/a"

UOM_BOOLEAN = "2"  # 0 = False / 1 = True
UOM_CLIMATE_MODES = "98"
UOM_CLIMATE_MODES_ZWAVE = "67"
UOM_DOUBLE_TEMP = "101"
UOM_FAN_MODES = "99"
UOM_INDEX = "25"
UOM_ON_OFF = "78"  # 0 = Off / 100 = On
UOM_OPEN_CLOSED = "79"  # 0 = Open / 100 = Closed
UOM_PERCENTAGE = "51"
UOM_RAW = "56"
UOM_SECONDS = "57"

UOM_TO_STATES: dict[str, dict[str, str]] = {
    "11": {  # Deadbolt Status
        "0": "unlocked",
        "100": "locked",
        "101": "unknown",
        "102": "problem",
    },
    "15": {  # Door Lock Alarm
        "1": "master code changed",
        "2": "tamper code entry limit",
        "3": "escutcheon removed",
        "4": "key-manually locked",
        "5": "locked by touch",
        "6": "key-manually unlocked",
        "7": "remote locking jammed bolt",
        "8": "remotely locked",
        "9": "remotely unlocked",
        "10": "deadbolt jammed",
        "11": "battery too low to operate",
        "12": "critical low battery",
        "13": "low battery",
        "14": "automatically locked",
        "15": "automatic locking jammed bolt",
        "16": "remotely power cycled",
        "17": "lock handling complete",
        "19": "user deleted",
        "20": "user added",
        "21": "duplicate pin",
        "22": "jammed bolt by locking with keypad",
        "23": "locked by keypad",
        "24": "unlocked by keypad",
        "25": "keypad attempt outside schedule",
        "26": "hardware failure",
        "27": "factory reset",
        "28": "manually not fully locked",
        "29": "all user codes deleted",
        "30": "new user code not added-duplicate code",
        "31": "keypad temporarily disabled",
        "32": "keypad busy",
        "33": "new program code entered",
        "34": "rf unlock with invalid user code",
        "35": "rf lock with invalid user codes",
        "36": "window-door is open",
        "37": "window-door is closed",
        "38": "window-door handle is open",
        "39": "window-door handle is closed",
        "40": "user code entered on keypad",
        "41": "power cycled",
    },
    "66": {  # Thermostat Heat/Cool State
        "0": "idle",
        "1": "heating",
        "2": "cooling",
        "3": "fan_only",
        "4": "pending heat",
        "5": "pending cool",
        "6": "vent",
        "7": "aux heat",
        "8": "2nd stage heating",
        "9": "2nd stage cooling",
        "10": "2nd stage aux heat",
        "11": "3rd stage aux heat",
    },
    "67": {  # Thermostat Mode
        "0": "off",
        "1": "heat",
        "2": "cool",
        "3": "auto",
        "4": "aux/emergency heat",
        "5": "resume",
        "6": "fan_only",
        "7": "furnace",
        "8": "dry air",
        "9": "moist air",
        "10": "auto changeover",
        "11": "energy save heat",
        "12": "energy save cool",
        "13": "away",
        "14": "program auto",
        "15": "program heat",
        "16": "program cool",
    },
    "68": {  # Thermostat Fan Mode
        "0": "auto",
        "1": "on",
        "2": "auto high",
        "3": "high",
        "4": "auto medium",
        "5": "medium",
        "6": "circulation",
        "7": "humidity circulation",
        "8": "left-right circulation",
        "9": "up-down circulation",
        "10": "quiet",
    },
    "78": {"0": "off", "100": "on"},  # 0-Off 100-On
    "79": {"0": "open", "100": "closed"},  # 0-Open 100-Close
    "80": {  # Thermostat Fan Run State
        "0": "off",
        "1": "on",
        "2": "on high",
        "3": "on medium",
        "4": "circulation",
        "5": "humidity circulation",
        "6": "right/left circulation",
        "7": "up/down circulation",
        "8": "quiet circulation",
    },
    "84": {"0": "unlock", "1": "lock"},  # Secure Mode
    "93": {  # Power Management Alarm
        "1": "power applied",
        "2": "ac mains disconnected",
        "3": "ac mains reconnected",
        "4": "surge detection",
        "5": "volt drop or drift",
        "6": "over current detected",
        "7": "over voltage detected",
        "8": "over load detected",
        "9": "load error",
        "10": "replace battery soon",
        "11": "replace battery now",
        "12": "battery is charging",
        "13": "battery is fully charged",
        "14": "charge battery soon",
        "15": "charge battery now",
    },
    "94": {  # Appliance Alarm
        "1": "program started",
        "2": "program in progress",
        "3": "program completed",
        "4": "replace main filter",
        "5": "failure to set target temperature",
        "6": "supplying water",
        "7": "water supply failure",
        "8": "boiling",
        "9": "boiling failure",
        "10": "washing",
        "11": "washing failure",
        "12": "rinsing",
        "13": "rinsing failure",
        "14": "draining",
        "15": "draining failure",
        "16": "spinning",
        "17": "spinning failure",
        "18": "drying",
        "19": "drying failure",
        "20": "fan failure",
        "21": "compressor failure",
    },
    "95": {  # Home Health Alarm
        "1": "leaving bed",
        "2": "sitting on bed",
        "3": "lying on bed",
        "4": "posture changed",
        "5": "sitting on edge of bed",
    },
    "96": {  # VOC Level
        "1": "clean",
        "2": "slightly polluted",
        "3": "moderately polluted",
        "4": "highly polluted",
    },
    "97": {  # Barrier Status
        "0": "closed",
        "100": "open",
        "101": "unknown",
        "102": "stopped",
        "103": "closing",
        "104": "opening",
        **{str(b): f"{b} %" for a, b in enumerate(list(range(1, 100)))},  # 1-99 are percentage open
    },
    "98": {  # Insteon Thermostat Mode
        "0": "off",
        "1": "heat",
        "2": "cool",
        "3": "auto",
        "4": "fan_only",
        "5": "program_auto",
        "6": "program_heat",
        "7": "program_cool",
    },
    "99": {"7": "on", "8": "auto"},  # Insteon Thermostat Fan Mode
    "115": {  # Most recent On style action taken for lamp control
        "0": "on",
        "1": "off",
        "2": "fade up",
        "3": "fade down",
        "4": "fade stop",
        "5": "fast on",
        "6": "fast off",
        "7": "triple press on",
        "8": "triple press off",
        "9": "4x press on",
        "10": "4x press off",
        "11": "5x press on",
        "12": "5x press off",
    },
}

# Translate the "RR" Property to Seconds
INSTEON_RAMP_RATES: dict[str, float] = {
    "0": 540,
    "1": 480,
    "2": 420,
    "3": 360,
    "4": 300,
    "5": 270,
    "6": 240,
    "7": 210,
    "8": 180,
    "9": 150,
    "10": 120,
    "11": 90,
    "12": 60,
    "13": 47,
    "14": 43,
    "15": 38.5,
    "16": 34,
    "17": 32,
    "18": 30,
    "19": 28,
    "20": 26,
    "21": 23.5,
    "22": 21.5,
    "23": 19,
    "24": 8.5,
    "25": 6.5,
    "26": 4.5,
    "27": 2,
    "28": 0.5,
    "29": 0.3,
    "30": 0.2,
    "31": 0.1,
}

# Insteon battery / stateless devices — motion sensors, RemoteLincs,
# binary-alarm nodedefs, etc. Their ``ST`` is not a persistent state, so
# ``runtime.Group`` skips these members when aggregating a scene's on/off
# state (see ``pyisyox.runtime.group``).
INSTEON_STATELESS_NODEDEFID: list[str] = [
    "BinaryAlarm",
    "BinaryAlarm_ADV",
    "BinaryControl",
    "BinaryControl_ADV",
    "RemoteLinc2",
    "RemoteLinc2_ADV",
    "DimmerSwitchOnly",
]

# Referenced from ISY-WSDK 4_fam.xml
# Included for user translations in external modules.
# This is the Node.zwave_props.category property.
DEVTYPE_CATEGORIES: dict[str, str] = {
    "0": "uninitialized",
    "101": "unknown",
    "102": "alarm",
    "103": "av control point",
    "104": "binary sensor",
    "105": "class a motor control",
    "106": "class b motor control",
    "107": "class c motor control",
    "108": "controller",
    "109": "dimmer switch",
    "110": "display",
    "111": "door lock",
    "112": "doorbell",
    "113": "entry control",
    "114": "gateway",
    "115": "installer tool",
    "116": "motor multiposition",
    "117": "climate sensor",
    "118": "multilevel sensor",
    "119": "multilevel switch",
    "120": "on/off power strip",
    "121": "on/off power switch",
    "122": "on/off scene switch",
    "123": "open/close valve",
    "124": "pc controller",
    "125": "remote",
    "126": "remote control",
    "127": "av remote control",
    "128": "simple remote control",
    "129": "repeater",
    "130": "residential hrv",
    "131": "satellite receiver",
    "132": "satellite receiver",
    "133": "scene controller",
    "134": "scene switch",
    "135": "security panel",
    "136": "set-top box",
    "137": "siren",
    "138": "smoke alarm",
    "139": "subsystem controller",
    "140": "thermostat",
    "141": "toggle",
    "142": "television",
    "143": "energy meter",
    "144": "pulse meter",
    "145": "water meter",
    "146": "gas meter",
    "147": "binary switch",
    "148": "binary alarm",
    "149": "aux alarm",
    "150": "co2 alarm",
    "151": "co alarm",
    "152": "freeze alarm",
    "153": "glass break alarm",
    "154": "heat alarm",
    "155": "motion sensor",
    "156": "smoke alarm",
    "157": "tamper alarm",
    "158": "tilt alarm",
    "159": "water alarm",
    "160": "door/window alarm",
    "161": "test alarm",
    "162": "low battery alarm",
    "163": "co end of life alarm",
    "164": "malfunction alarm",
    "165": "heartbeat",
    "166": "overheat alarm",
    "167": "rapid temp rise alarm",
    "168": "underheat alarm",
    "169": "leak detected alarm",
    "170": "level drop alarm",
    "171": "replace filter alarm",
    "172": "intrusion alarm",
    "173": "tamper code alarm",
    "174": "hardware failure alarm",
    "175": "software failure alarm",
    "176": "contact police alarm",
    "177": "contact fire alarm",
    "178": "contact medical alarm",
    "179": "wakeup alarm",
    "180": "timer",
    "181": "power management",
    "182": "appliance",
    "183": "home health",
    "184": "barrier",
    "185": "notification sensor",
    "186": "color switch",
    "187": "multilevel switch off on",
    "188": "multilevel switch down up",
    "189": "multilevel switch close open",
    "190": "multilevel switch ccw cw",
    "191": "multilevel switch left right",
    "192": "multilevel switch reverse forward",
    "193": "multilevel switch pull push",
    "194": "basic set",
    "195": "wall controller",
    "196": "barrier handle",
    "197": "sound switch",
}

# Referenced from ISY-WSDK cat.xml
# Included for user translations in external modules.
# This is the first part of the Node.type property (before the first ".")
NODE_CATEGORIES: dict[str, str] = {
    "0": "generic controller",
    "1": "dimming control",
    "2": "switch/relay control",
    "3": "network bridge",
    "4": "irrigation control",
    "5": "climate control",
    "6": "pool control",
    "7": "sensors/actuators",
    "8": "home entertainment",
    "9": "energy management",
    "10": "appliance control",
    "11": "plumbing",
    "12": "communications",
    "13": "computer",
    "14": "windows/shades",
    "15": "access control",
    "16": "security/health/safety",
    "17": "surveillance",
    "18": "automotive",
    "19": "pet care",
    "20": "toys",
    "21": "timers/clocks",
    "22": "holiday",
    "113": "a10/x10",
    "127": "virtual",
    "254": "unknown",
}


class SystemStatus(StrEnum):
    """System Status Enum — the ``<action>`` value on ``_5`` event frames."""

    NOT_BUSY = "0"
    BUSY = "1"
    IDLE = "2"
    SAFE_MODE = "3"

    @classmethod
    def label(cls, value: str) -> str:
        """Friendly lower-case name for a system-status value, or the
        raw value verbatim if it isn't one we know.

        Mirrors :meth:`pyisyox.runtime.SystemEventControl.label` so the
        two compose cleanly in log lines
        (``system_status = not_busy``).
        """
        try:
            return cls(value).name.lower()
        except ValueError:
            return value


class NodeFlag(IntFlag):
    """Node operations flag enum."""

    INIT = 0x01  # needs to be initialized
    TO_SCAN = 0x02  # needs to be scanned
    GROUP = 0x04  # it’s a group!
    ROOT = 0x08  # it’s the root group
    IN_ERR = 0x10  # it’s in error!
    NEW = 0x20  # brand new node
    TO_DELETE = 0x40  # has to be deleted later
    DEVICE_ROOT = 0x80  # root device such as KPL load


DEV_CMD_MEMORY_WRITE = "0x2E"
DEV_BL_ADDR = "0x0264"
DEV_OL_ADDR = "0x0032"
DEV_RR_ADDR = "0x0021"

BACKLIGHT_SUPPORT: dict[str, str] = {
    "DimmerMotorSwitch": UOM_PERCENTAGE,
    "DimmerMotorSwitch_ADV": UOM_PERCENTAGE,
    "DimmerLampSwitch": UOM_PERCENTAGE,
    "DimmerLampSwitch_ADV": UOM_PERCENTAGE,
    "DimmerSwitchOnly": UOM_PERCENTAGE,
    "DimmerSwitchOnly_ADV": UOM_PERCENTAGE,
    "KeypadDimmer": UOM_INDEX,
    "KeypadDimmer_ADV": UOM_INDEX,
    "RelayLampSwitch": UOM_PERCENTAGE,
    "RelayLampSwitch_ADV": UOM_PERCENTAGE,
    "RelaySwitchOnlyPlusQuery": UOM_PERCENTAGE,
    "RelaySwitchOnlyPlusQuery_ADV": UOM_PERCENTAGE,
    "RelaySwitchOnly": UOM_PERCENTAGE,
    "RelaySwitchOnly_ADV": UOM_PERCENTAGE,
    "KeypadRelay": UOM_INDEX,
    "KeypadRelay_ADV": UOM_INDEX,
    "KeypadButton": UOM_INDEX,
    "KeypadButton_ADV": UOM_INDEX,
}

BACKLIGHT_INDEX: list[str] = [
    "On  0 / Off 0",
    "On  1 / Off 0",
    "On  2 / Off 0",
    "On  3 / Off 0",
    "On  4 / Off 0",
    "On  5 / Off 0",
    "On  6 / Off 0",
    "On  7 / Off 0",
    "On  8 / Off 0",
    "On  9 / Off 0",
    "On 10 / Off 0",
    "On 11 / Off 0",
    "On 12 / Off 0",
    "On 13 / Off 0",
    "On 14 / Off 0",
    "On 15 / Off 0",
    "On  0 / Off 1",
    "On  1 / Off 1",
    "On  2 / Off 1",
    "On  3 / Off 1",
    "On  4 / Off 1",
    "On  5 / Off 1",
    "On  6 / Off 1",
    "On  7 / Off 1",
    "On  8 / Off 1",
    "On  9 / Off 1",
    "On 10 / Off 1",
    "On 11 / Off 1",
    "On 12 / Off 1",
    "On 13 / Off 1",
    "On 14 / Off 1",
    "On 15 / Off 1",
    "On  0 / Off 2",
    "On  1 / Off 2",
    "On  2 / Off 2",
    "On  3 / Off 2",
    "On  4 / Off 2",
    "On  5 / Off 2",
    "On  6 / Off 2",
    "On  7 / Off 2",
    "On  8 / Off 2",
    "On  9 / Off 2",
    "On 10 / Off 2",
    "On 11 / Off 2",
    "On 12 / Off 2",
    "On 13 / Off 2",
    "On 14 / Off 2",
    "On 15 / Off 2",
    "On  0 / Off 3",
    "On  1 / Off 3",
    "On  2 / Off 3",
    "On  3 / Off 3",
    "On  4 / Off 3",
    "On  5 / Off 3",
    "On  6 / Off 3",
    "On  7 / Off 3",
    "On  8 / Off 3",
    "On  9 / Off 3",
    "On 10 / Off 3",
    "On 11 / Off 3",
    "On 12 / Off 3",
    "On 13 / Off 3",
    "On 14 / Off 3",
    "On 15 / Off 3",
    "On  0 / Off 4",
    "On  1 / Off 4",
    "On  2 / Off 4",
    "On  3 / Off 4",
    "On  4 / Off 4",
    "On  5 / Off 4",
    "On  6 / Off 4",
    "On  7 / Off 4",
    "On  8 / Off 4",
    "On  9 / Off 4",
    "On 10 / Off 4",
    "On 11 / Off 4",
    "On 12 / Off 4",
    "On 13 / Off 4",
    "On 14 / Off 4",
    "On 15 / Off 4",
    "On  0 / Off 5",
    "On  1 / Off 5",
    "On  2 / Off 5",
    "On  3 / Off 5",
    "On  4 / Off 5",
    "On  5 / Off 5",
    "On  6 / Off 5",
    "On  7 / Off 5",
    "On  8 / Off 5",
    "On  9 / Off 5",
    "On 10 / Off 5",
    "On 11 / Off 5",
    "On 12 / Off 5",
    "On 13 / Off 5",
    "On 14 / Off 5",
    "On 15 / Off 5",
    "On  0 / Off 6",
    "On  1 / Off 6",
    "On  2 / Off 6",
    "On  3 / Off 6",
    "On  4 / Off 6",
    "On  5 / Off 6",
    "On  6 / Off 6",
    "On  7 / Off 6",
    "On  8 / Off 6",
    "On  9 / Off 6",
    "On 10 / Off 6",
    "On 11 / Off 6",
    "On 12 / Off 6",
    "On 13 / Off 6",
    "On 14 / Off 6",
    "On 15 / Off 6",
    "On  0 / Off 7",
    "On  1 / Off 7",
    "On  2 / Off 7",
    "On  3 / Off 7",
    "On  4 / Off 7",
    "On  5 / Off 7",
    "On  6 / Off 7",
    "On  7 / Off 7",
    "On  8 / Off 7",
    "On  9 / Off 7",
    "On 10 / Off 7",
    "On 11 / Off 7",
    "On 12 / Off 7",
    "On 13 / Off 7",
    "On 14 / Off 7",
    "On 15 / Off 7",
]


class UDHierarchyNodeType(IntEnum):
    """Enum representation of node types."""

    NOTSET = 0
    NODE = 1
    GROUP = 2
    FOLDER = 3
