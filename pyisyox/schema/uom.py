"""Unit-of-measure table for IoX 6.

Each editor range carries a ``uom`` id (string) that resolves into this
table. ``category_id`` is a coarse semantic grouping (temperature, volume,
electric_current, etc.) used by HA's platform classifier to decide between
``sensor`` device classes.

This table is the canonical UOM source for pyisyox; it covers every
UOM UDI publishes plus the friendly short-symbol form HA typically
wants for ``unit_of_measurement``. ``name`` holds the short symbol
(e.g. ``"°C"``, ``"%"``, ``"A"``); ``description`` is UDI's verbose
label.

Sources merged: UDI's nucore-ai ``src/nucore/uom.py`` for description
+ category, plus the legacy pyisyox ``UOM_FRIENDLY_NAME`` table for
short-symbol display strings (the values HA users see). Where the two
disagreed on the symbol form, ``UOM_FRIENDLY_NAME`` won — it was
hand-curated to match HA's conventions.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UOMEntry:
    """A unit-of-measure entry.

    Attributes:
        id: UOM identifier as it appears in profile responses.
        name: Short display name (suitable for sensor unit_of_measurement).
        description: Free-text description.
        category_id: Coarse semantic category (see module docstring).
    """

    id: str
    name: str
    description: str = ""
    category_id: str | None = None


UNKNOWN_UOM = "0"


PREDEFINED_UOMS: dict[str, UOMEntry] = {
    "0": UOMEntry("0", "", "The unit of measure is unknown"),
    "1": UOMEntry("1", "A", "Electrical current in Amperes", "electric_current"),
    "2": UOMEntry("2", "", "A boolean value where 0 = False, 1 = True"),
    "3": UOMEntry("3", "btu/h", "BTU/Hour", "power"),
    "4": UOMEntry("4", "°C", "Degree of temperature in Celsius", "temperature"),
    "5": UOMEntry("5", "cm", "Centimeters", "distance"),
    "6": UOMEntry("6", "ft³", "Cubic Feet", "volume"),
    "7": UOMEntry("7", "ft³/min", "Cubic Feet/Minute", "volume_flow"),
    "8": UOMEntry("8", "m³", "Cubic Meter", "volume"),
    "9": UOMEntry("9", "day", "Day of the Month"),
    "10": UOMEntry("10", "days", "Duration in Days", "time_duration"),
    "11": UOMEntry("11", "", "The position of the Deadbolt"),
    "12": UOMEntry("12", "dB", "The number of Decibels"),
    "13": UOMEntry("13", "dB A", "The number of A-weighted Decibels"),
    "14": UOMEntry("14", "°", "Generic Degree of temperature", "temperature"),
    "15": UOMEntry("15", "", "Door Lock alarm type"),
    "16": UOMEntry("16", "macroseismic", "European macroseismic"),
    "17": UOMEntry("17", "°F", "Degree of temperature in Fahrenheit", "temperature"),
    "18": UOMEntry("18", "ft", "Feet", "distance"),
    "19": UOMEntry("19", "hour", "Hour on the clock"),
    "20": UOMEntry("20", "hours", "A duration in Hours", "time_duration"),
    "21": UOMEntry("21", "%AH", "The Absolute Humidity"),
    "22": UOMEntry("22", "%RH", "The Relative Humidity"),
    "23": UOMEntry("23", "inHg", "Inches of Mercury (inHg)", "pressure"),
    "24": UOMEntry("24", "in/hr", "Inches per hour", "speed"),
    "25": UOMEntry("25", "index", "The list index of a value for a given list of values."),
    "26": UOMEntry("26", "K", "Degree of temperature in Kelvin", "temperature"),
    "27": UOMEntry("27", "keyword", "Keyword"),
    "28": UOMEntry("28", "kg", "Weight in Kilograms", "weight"),
    "29": UOMEntry("29", "kV", "Kilovolts", "voltage"),
    "30": UOMEntry("30", "kW", "Kilowatts", "power"),
    "31": UOMEntry("31", "kPa", "Kilopascals", "pressure"),
    "32": UOMEntry("32", "KPH", "Kilometers/Hour", "speed"),
    "33": UOMEntry("33", "kWh", "Kilowatt hour", "energy"),
    "34": UOMEntry("34", "liedu", "Liedu seismic intensity scale"),
    "35": UOMEntry("35", "L", "Liter", "volume"),
    "36": UOMEntry("36", "lx", "Measure of light in lux"),
    "37": UOMEntry("37", "mercalli", "Mercalli seismic intensity scale"),
    "38": UOMEntry("38", "m", "Meter", "distance"),
    "39": UOMEntry("39", "m³/hr", "Number of Cubic Meters per Hour", "volume_flow"),
    "40": UOMEntry("40", "m/s", "Number of meters per second", "speed"),
    "41": UOMEntry("41", "mA", "Milliamp", "electric_current"),
    "42": UOMEntry("42", "ms", "Millisecond on the clock", "time_duration"),
    "43": UOMEntry("43", "mV", "Millivolt", "voltage"),
    "44": UOMEntry("44", "min", "Minute on the clock", "time_duration"),
    "45": UOMEntry("45", "min", "Duration in minutes", "time_duration"),
    "46": UOMEntry("46", "mm/hr", "Millimeters/hour", "speed"),
    "47": UOMEntry("47", "month", "Month"),
    "48": UOMEntry("48", "MPH", "Miles/Hour", "speed"),
    "49": UOMEntry("49", "m/s", "Meters per second", "speed"),
    "50": UOMEntry("50", "Ω", "Electrical resistance in Ohms", "resistance"),
    "51": UOMEntry("51", "%", "Percent"),
    "52": UOMEntry("52", "lbs", "Weight in Pounds", "weight"),
    "53": UOMEntry("53", "pf", "Power factor"),
    "54": UOMEntry("54", "ppm", "Parts Per Million"),
    "55": UOMEntry("55", "pulse count", "Pulse Count"),
    "56": UOMEntry("56", "", "The raw value used by the device"),
    "57": UOMEntry("57", "s", "Second on a clock", "time_duration"),
    "58": UOMEntry("58", "s", "Duration in seconds", "time_duration"),
    "59": UOMEntry("59", "S/m", "Siemens per meter"),
    "60": UOMEntry("60", "m_b", "Body Wave Magnitude Scale"),
    "61": UOMEntry("61", "M_L", "Seismic activity level using the Richter Scale"),
    "62": UOMEntry("62", "M_w", "Moment Magnitude Scale"),
    "63": UOMEntry("63", "M_S", "Surface Wave Magnitude Scale"),
    "64": UOMEntry("64", "shindo", "Shindo seismic activity scale"),
    "65": UOMEntry("65", "SML", "Reserved for future use"),
    "66": UOMEntry("66", "", "Heating/Cooling state of the thermostat"),
    "67": UOMEntry("67", "", "Thermostat operational mode"),
    "68": UOMEntry("68", "", "Thermostat fan mode"),
    "69": UOMEntry("69", "gal", "US Gallons", "volume"),
    "70": UOMEntry("70", "", "A number identifying a user"),
    "71": UOMEntry("71", "UV index", "Ultraviolet Index"),
    "72": UOMEntry("72", "V", "Volts", "voltage"),
    "73": UOMEntry("73", "W", "Power in Watts", "power"),
    "74": UOMEntry("74", "W/m²", "Watts per square meter"),
    "75": UOMEntry("75", "weekday", "Weekday"),
    "76": UOMEntry("76", "°", "A 1-360 degree clockwise Wind Direction, 0 indicates no wind", "direction"),
    "77": UOMEntry("77", "year", "Year"),
    "78": UOMEntry("78", "", "On or off, where Off=0, On=100, Unknown=101"),
    "79": UOMEntry("79", "", "Open or Closed, where Open=0, Closed=100, Unknown=101"),
    "80": UOMEntry("80", "", "The running state of the Fan"),
    "81": UOMEntry("81", "", "Fan Mode Override"),
    "82": UOMEntry("82", "mm", "Millimeter", "distance"),
    "83": UOMEntry("83", "km", "Kilometer", "distance"),
    "84": UOMEntry("84", "", "Secure Mode"),
    "85": UOMEntry("85", "Ω", "Resistivity in ohm-meters"),
    "86": UOMEntry("86", "kΩ", "KiloOhm", "resistance"),
    "87": UOMEntry("87", "m³/m³", "Cubic Meter/Cubic Meter"),
    "88": UOMEntry("88", "Water activity", "Water Activity"),
    "89": UOMEntry("89", "RPM", "Rotations/Minute (RPM)"),
    "90": UOMEntry("90", "Hz", "Frequency in Hertz (1 hertz = one cycle per second)", "frequency"),
    "91": UOMEntry("91", "°", "Degrees relative to north pole of standing eye view", "direction"),
    "92": UOMEntry("92", "° South", "Degrees relative to south pole of standing eye view", "direction"),
    "93": UOMEntry("93", "", "Power Management Alarm"),
    "94": UOMEntry("94", "", "Appliance Alarm"),
    "95": UOMEntry("95", "", "Home Health Alarm"),
    "96": UOMEntry("96", "", "Volatile Organic Compound (VOC) Level"),
    "97": UOMEntry("97", "%", "Barrier Status"),
    "98": UOMEntry("98", "", "Insteon Thermostat Mode"),
    "99": UOMEntry("99", "", "Insteon Thermostat Fan Mode"),
    "100": UOMEntry("100", "", "A Level from 0-255 (for example, the brightness of a dimmable lamp)"),
    "101": UOMEntry("101", "° (x2)", "Degree multiplied by 2"),
    "102": UOMEntry("102", "kWs", "Kilowatt second", "energy"),
    "103": UOMEntry("103", "$", "Dollars"),
    "104": UOMEntry("104", "¢", "Cents"),
    "105": UOMEntry("105", "in", "Inches", "distance"),
    "106": UOMEntry("106", "mm/day", "Millimeters per Day", "speed"),
    "107": UOMEntry("107", "", "Raw 1-Byte unsigned value"),
    "108": UOMEntry("108", "", "Raw 2-Byte unsigned value"),
    "109": UOMEntry("109", "", "Raw 3-Byte unsigned value"),
    "110": UOMEntry("110", "", "Raw 4-Byte unsigned value"),
    "111": UOMEntry("111", "", "Raw 1-Byte signed value"),
    "112": UOMEntry("112", "", "Raw 2-Byte signed value"),
    "113": UOMEntry("113", "", "Raw 3-Byte signed value"),
    "114": UOMEntry("114", "", "Raw 4-Byte signed value"),
    "115": UOMEntry("115", "", "Most recent On style action taken for lamp control"),
    "116": UOMEntry("116", "mi", "Miles", "distance"),
    "117": UOMEntry("117", "mbar", "Millibars, typically used in barometric reports", "pressure"),
    "118": UOMEntry("118", "hPa", "Hectopascals, typically used in barometric reports", "pressure"),
    "119": UOMEntry("119", "Wh", "Watt Hour", "energy"),
    "120": UOMEntry("120", "in/day", "Inches per day", "speed"),
    "121": UOMEntry("121", "mol/m³", "Mole per cubic meter (mol/m3)"),
    "122": UOMEntry("122", "μg/m³", "Microgram per cubic meter (µg/m³)"),
    "123": UOMEntry("123", "bq/m³", "Becquerel per cubic meter (bq/m³)"),
    "124": UOMEntry("124", "pCi/L", "Picocuries per liter (pCi/l)"),
    "125": UOMEntry("125", "pH", "Acidity (pH)"),
    "126": UOMEntry("126", "bpm", "Beats per Minute (bpm)"),
    "127": UOMEntry("127", "mmHg", "Millimeters of mercury (mmHg)", "pressure"),
    "128": UOMEntry("128", "J", "Joule (J)", "energy"),
    "129": UOMEntry("129", "BMI", "Body Mass Index (BMI)"),
    "130": UOMEntry("130", "L/h", "Liters per hour (l/h)", "volume_flow"),
    "131": UOMEntry("131", "dBm", "Decibel Milliwatts (dBm)"),
    "132": UOMEntry("132", "bpm", "Breaths per minute (brpm)"),
    "133": UOMEntry("133", "kHz", "Kilohertz (kHz)", "frequency"),
    "134": UOMEntry("134", "m/²", "Meters per squared Seconds (m/sec2)"),
    "135": UOMEntry("135", "VA", "Volt-Amp (VA)"),
    "136": UOMEntry("136", "var", "Volt-Amp Reactive"),
    "137": UOMEntry("137", "", "NTP Date/Time"),
    "138": UOMEntry("138", "psi", "Pound per square inch (PSI)", "pressure"),
    "139": UOMEntry("139", "°", "Direction 0-360 degrees", "direction"),
    "140": UOMEntry("140", "mg/L", "Milligram per liter (mg/l)"),
    "141": UOMEntry("141", "N", "Newton"),
    "142": UOMEntry("142", "gal/s", "US Gallons per second", "volume_flow"),
    "143": UOMEntry("143", "gpm", "US Gallons per minute (gpm)", "volume_flow"),
    "144": UOMEntry("144", "gph", "US Gallons per hour", "volume_flow"),
    "145": UOMEntry("145", "Text", "Text"),
    "146": UOMEntry("146", "Notification ID", "Short Notification ID"),
    "147": UOMEntry("147", "XML", "XML"),
    "148": UOMEntry("148", "Notification ID", "Full Notification ID"),
    "149": UOMEntry("149", "°", "Hue in Degrees", "direction"),
    "150": UOMEntry("150", "URL Stream", "URL data stream"),
    "151": UOMEntry(
        "151", "Unix Timestamp", "Unix Timestamp (seconds since Jan 1/1970, UTC)", "time_duration"
    ),
    "152": UOMEntry("152", "Mired", "Mired (color temperature)"),
    "153": UOMEntry("153", "Color", "Color XY (usually a value between 0.00000 to 1.00000)"),
    "154": UOMEntry("154", "Steps / Second", "Number of steps per second"),
}


def get_uom(uom_id: str) -> UOMEntry:
    """Look up a UOM by id; returns the unknown entry if unrecognised."""
    return PREDEFINED_UOMS.get(uom_id, PREDEFINED_UOMS[UNKNOWN_UOM])
