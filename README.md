## PyISYoX

### Python Library for the ISY / IoX Controllers

ISY is a home automation controller capable of controlling Insteon, X10, Z-Wave and Zigbee/Matter devices connected to supported hardware manufactured by [Universal Devices, Inc.](https://www.universal-devices.com/).

This Python library supports the legacy ISY994 hardware family, as well as current ISY-on-Anything (IoX) hardware, such as the [eisy](https://www.universal-devices.com/product/eisy-home/) or [Polisy](https://www.universal-devices.com/polisy/) devices.

This library allows for easy asynchronous interaction with ISY nodes, programs, variables, node servers, and the networking module, including methods to monitor and automatically update when ISY parameters are changed.

The PyISYoX library originated from [PyISY](https://github.com/automicus/PyISY), originally authored by Ryan Kraus ([@rmkraus]) and maintained by Greg Laabs ([@overloadut]). The PyISYoX module has been rewritten by [@shbatm] using the original principles of PyISY, but redesigned based on the codebase of it's primary use case as a [Home Assistant](https://github.com/home-assistant/core) [Integration](https://www.home-assistant.io/integrations/isy994/).

**Disclaimer**: This project has no affiliation with [Universal Devices, Inc.](https://www.universal-devices.com/), it is maintained independently and is not officially supported in anyway by the OEM. Any issues with the module must be raised here, and will be resolved as quickly as possible given limited resources. Any issues with the controllers themselves should be directed to UDI.

### Requirements

The minimum required Python version is 3.10

### Examples

To test run the module, print a summary of your connected nodes, and log the events from the event websocket:

```shell
pip install pyisyox
python3 -m pyisyox http://polisy.local:8080 admin password
```

See the [examples](examples/) folder for connection examples.

Partial documentation is available at https://pyisyox.readthedocs.io. This is being updated as time allows.

### Contributing

A note on contributing: contributions of any sort are more than welcome!

A [VSCode DevContainer](https://code.visualstudio.com/docs/remote/containers#_getting-started) is available to provide a consistent development environment.

Assuming you have the pre-requisites installed from the link above (VSCode, Docker, & Remote-Containers Extension), to get started:

1. Fork the repository.
2. Clone the repository to your computer.
3. Open the repository using Visual Studio code.
4. When you open this repository with Visual Studio code you are asked to "Reopen in Container", this will start the build of the container.
   - If you don't see this notification, open the command palette and select Remote-Containers: Reopen Folder in Container.
5. Once started, you will also have a `examples/` folder with a copy of the example scripts to run in the container which won't be committed to the repo, so you can update them with your connection details and test directly on your ISY.

[@overloadut]: https://github.com/overloadut
[@rmkraus]: https://github.com/rmkraus
[@shbatm]: https://github.com/shbatm
