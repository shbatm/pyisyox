PyISYoX
=====

A Python Library for the ISY/IoX Controllers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

This module was developed to communicate with the `UDI ISY <https://www.universal-devices.com/>`_
home automation hub via the hub's REST interface and Websocket/SOAP event streams. It provides
near real-time updates from the device and allows control of all devices that
are supported within the ISY.

This module also allows for functions to be assigned as handlers when ISY parameters are changed.
ISY parameters can be monitored automatically as changes are reported from the device.

.. warning::

    THIS DOCUMENTATION IS STILL A WORK-IN-PROGRESS. Some of the details have not yet been updated
    for Version 2 or Version 3 of the PyISYoX Module. If you would like to help, please contribute
    on GitHub.


Project Information
~~~~~~~~~~~~~~~~~~~

.. note::

    This documentation is specific to PyISYoX Version 4, which uses asynchronous
    communications and the asyncio module. If you need threaded (synchronous) support
    please use Version 2.x.x.

|  Docs: `ReadTheDocs <https://pyisyox.readthedocs.io>`_
|  Source: `GitHub <https://github.com/automicus/PyISYoX>`_


Installation
~~~~~~~~~~~~

The easiest way to install this package is using pip with the command:

.. code-block:: bash

    pip3 install pyisyox

See the :ref:`PyISYoX Tutorial<tutorial>` for guidance on how to use the module.

Requirements
~~~~~~~~~~~~

This package requires three other packages, also available from pip. They are
installed automatically when PyISYoX is installed using pip.

* `requests <http://docs.python-requests.org/en/latest/>`_
* `dateutil <https://dateutil.readthedocs.io/en/stable/>`_
* `aiohttp <https://docs.aiohttp.org/en/stable/>`_

Contents
========

.. toctree::
    :maxdepth: 1
    :name: mastertoc
    :glob:

    *
    api/index.rst

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
