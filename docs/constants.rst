Constants
=========

The :mod:`pyisyox.constants` module collects the wire-level constants
PyISYoX uses to talk to the eisy: command ids (``DON``, ``DOF``,
``CLISPC``, ...), property ids (``ST``, ``OL``, ``RR``, ...), node
families, system-status codes, UOM constants, and a few enums that
classify states or stream lifecycle.

Most consumers should not need to import from here directly — the
runtime wrappers expose the values through typed properties — but
the enums and a few constants do appear in public API signatures.

Enums
-----

The constants module defines several :class:`enum.StrEnum` /
:class:`enum.IntEnum` / :class:`enum.IntFlag` types that show up in
event handlers and node introspection:

.. autoclass:: pyisyox.constants.EventStreamStatus
    :members:

.. autoclass:: pyisyox.constants.Protocol
    :members:

.. autoclass:: pyisyox.constants.NodeFamily
    :members:

.. autoclass:: pyisyox.constants.SystemStatus
    :members:

.. autoclass:: pyisyox.constants.NodeFlag
    :members:

.. autoclass:: pyisyox.constants.UDHierarchyNodeType
    :members:

Command and property identifiers
--------------------------------

Wire-level command ids (``CMD_*``) and property ids (``PROP_*``) are
string constants. Use them when calling :meth:`~pyisyox.Node.send_command`
to avoid typos:

.. code-block:: python

    from pyisyox.constants import CMD_BACKLIGHT, PROP_ON_LEVEL

    await node.send_command(CMD_BACKLIGHT, "Medium")
    await node.send_command(PROP_ON_LEVEL, 200)

Full reference
--------------

The complete list of constants — including command ids, property ids,
attribute / tag names used during XML parsing, default precision and
UOM constants, and the canonical UDI source references — is documented
inline:

.. automodule:: pyisyox.constants
    :members:
    :no-index:

The constants are derived from UDI's
`IoX REST Developer Reference <https://www.universal-devices.com/developers/>`_
and the legacy
`ISY994 Developer Cookbook <https://www.universal-devices.com/docs/production/The+ISY994+Developer+Cookbook.pdf>`_.
PyISYoX targets IoX 6.0.0+; entries that only existed on the original
ISY-994 hardware are preserved for compatibility but are not exercised
by the library.
