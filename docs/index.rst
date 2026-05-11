PyISYoX
=======

An async Python client for eisy / Polisy on IoX 6
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

PyISYoX talks to Universal Devices' **eisy** and **Polisy** controllers
running **IoX 6.0.0+**. It is a JSON-first rewrite of the original
``pyisy`` library, built around a small set of focused public types:

* :class:`pyisyox.Controller` — the one user-facing handle. Construct
  it, ``await controller.connect()``, drive nodes through
  ``controller.nodes[address].send_command(...)``, subscribe to
  WebSocket events, then ``await controller.stop()``.
* :class:`pyisyox.PortalAuth` / :class:`pyisyox.LocalAuth` — auth
  strategies. PortalAuth (JWT bearer) is the recommended default;
  LocalAuth (HTTP basic) exists as a feature-degraded fallback.
* :class:`pyisyox.Node`, :class:`pyisyox.Group`,
  :class:`pyisyox.Program`, :class:`pyisyox.Variable`,
  :class:`pyisyox.NetworkResource` — runtime wrappers over the
  controller's domain objects. WebSocket frames mutate their
  underlying records in place, so attribute reads always reflect the
  latest state.

.. note::

   The original ISY-994 hardware is **out of scope**. It tops out at
   TLS 1.1 and a much smaller REST surface; users on that hardware
   should use the upstream ``pyisy`` (v3.x) library. Everything here
   assumes IoX 6 on eisy or Polisy.

Quick example
-------------

.. code-block:: python

    import asyncio
    from pyisyox import Controller, PortalAuth

    async def main() -> None:
        controller = Controller(
            "https://eisy.local:443",
            PortalAuth("me@example.com", "portal-password"),
        )
        await controller.connect()
        try:
            light = controller.nodes["3D 7D 87 1"]
            await light.send_command("DON", 75)  # 75% on
        finally:
            await controller.stop()

    asyncio.run(main())

See :doc:`quickstart` for a guided tour, :doc:`connection-flow` for
the wire-level connect sequence, and :doc:`events` for the
WebSocket dispatcher and subscription API.

Installation
------------

.. code-block:: shell

    pip install pyisyox

Requirements: **Python 3.11+** and ``aiohttp``. No XML parser
dependencies — the narrow XML surfaces still in use (``/rest/status``,
``/rest/nodes``, command responses, WS event frames) are decoded with
``xml.etree.ElementTree`` from the stdlib.

.. _project_information:

Project information
-------------------

| Source: `GitHub <https://github.com/shbatm/pyisyox>`_
| PyPI: `pyisyox <https://pypi.org/project/pyisyox/>`_
| Docs: `ReadTheDocs <https://pyisyox.readthedocs.io>`_

Contents
========

.. toctree::
    :maxdepth: 2
    :name: mastertoc

    quickstart
    library
    connection-flow
    events
    constants
    api/index

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
