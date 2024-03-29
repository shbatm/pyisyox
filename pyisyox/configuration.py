"""ISY Configuration Lookup."""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from pyisyox.constants import (
    ATTR_ID,
    CONFIG_NETWORKING,
    CONFIG_PORTAL,
    DEFAULT_DIR,
    TAG_DESC,
    TAG_FEATURE,
    TAG_FIRMWARE,
    TAG_INSTALLED,
    TAG_NAME,
    TAG_NODE_DEFS,
    TAG_PRODUCT,
    TAG_ROOT,
    TAG_VARIABLES,
    URL_CONFIG,
)
from pyisyox.helpers.xml import parse_xml
from pyisyox.logging import _LOGGER
from pyisyox.util.output import write_to_file

if TYPE_CHECKING:
    from pyisyox.connection import Connection

PLATFORM = "config"
TRUE = "true"
TAG_FEATURES = "features"
TAG_CONFIG = "configuration"
TAG_PLATFORM = "platform"


@dataclass
class ConfigurationData:
    """ISY Configuration Dataclass.

    DESCRIPTION:
        This class handles the ISY configuration.

    USAGE:
        This object may be used in a similar way as a
        dictionary with the either module names or ids
        being used as keys and a boolean indicating
        whether the module is installed will be
        returned. With the exception of 'firmware' and 'uuid',
        which will return their respective values.

    FEATURES:
        Portal Integration - Check-it.ca
        Gas Meter
        SEP ESP
        Water Meter
        Z-Wave
        RCS Zigbee Device Support
        Irrigation/ETo Module
        Electricity Monitor
        AMI Electricity Meter
        URL
        A10/X10 for INSTEON
        Portal Integration - GreenNet.com
        Networking Module
        OpenADR
        Current Cost Meter
        Weather Information
        Broadband SEP Device
        Portal Integration - BestBuy.com
        Elk Security System
        Portal Integration - MobiLinc
        NorthWrite NOC Module
    """

    config: dict
    firmware: str
    uuid: str
    name: str
    model: str
    platform: str
    variables: bool
    nodedefs: bool
    networking: bool
    portal: bool
    features: list[dict[str, str]]
    node_servers: bool = False

    def __getitem__(self, item: str) -> Any:
        """Make subscriptable for backwards compatibility."""
        return getattr(self, item)


class Configuration:
    """Class to update the ISY configuration information."""

    config_data: ConfigurationData = None  # type: ignore[assignment]

    async def update(self, conn: Connection, wait_time: float = 0) -> ConfigurationData:
        """Update the contents of the networking class."""
        await asyncio.sleep(wait_time)
        xml_dict = parse_xml(
            await conn.request(conn.compile_url([URL_CONFIG])),
            raise_on_error=True,
            use_pp=False,
        )
        config = xml_dict[TAG_CONFIG]
        features = config[TAG_FEATURES][TAG_FEATURE]
        networking = any(
            i
            for i in features
            if i[TAG_DESC] == CONFIG_NETWORKING and i[TAG_INSTALLED] == TRUE
        )
        portal = any(
            i
            for i in features
            if i[TAG_DESC] == CONFIG_PORTAL and i[TAG_INSTALLED] == TRUE
        )

        self.config_data = ConfigurationData(
            config=config,
            firmware=config[TAG_FIRMWARE],
            uuid=config[TAG_ROOT][ATTR_ID],
            name=config[TAG_ROOT][TAG_NAME],
            model=config[TAG_PRODUCT][TAG_DESC],
            platform=config[TAG_PLATFORM],
            variables=bool(config[TAG_VARIABLES] == TRUE),
            nodedefs=bool(config[TAG_NODE_DEFS] == TRUE),
            networking=networking,
            portal=portal,
            features=features,
        )

        if conn.args is not None and conn.args.file:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(
                None,
                write_to_file,
                asdict(self.config_data),
                f"{DEFAULT_DIR}rest-{PLATFORM}.yaml",
            )

        _LOGGER.info("Loaded configuration")
        return self.config_data

    def __str__(self) -> str:
        """Return string representation of Configuration data."""
        return str(asdict(self.config_data))

    def __repr__(self) -> str:
        """Return string representation of Configuration data."""
        return repr(self.config_data)
