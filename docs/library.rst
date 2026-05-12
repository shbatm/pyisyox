Library Reference
=================

PyISYoX's public surface is small and layered. From most to least
"glue":

* :class:`~pyisyox.Controller` — the user-facing handle that composes
  everything else.
* Runtime wrappers — :class:`~pyisyox.Node`, :class:`~pyisyox.Group`,
  :class:`~pyisyox.Folder`, :class:`~pyisyox.Program`,
  :class:`~pyisyox.ProgramFolder`, :class:`~pyisyox.Variable`,
  :class:`~pyisyox.NetworkResource`. Each shares its underlying record
  with the controller's loaded state.
* Auth strategies — :class:`~pyisyox.PortalAuth`,
  :class:`~pyisyox.LocalAuth`, :class:`~pyisyox.Auth`.
* HTTP client — :class:`~pyisyox.IoXClient` and the record dataclasses
  it produces (:class:`~pyisyox.ControllerConfig`,
  :class:`~pyisyox.NodeRecord`, etc.). Consumers rarely need to
  touch these directly; the :class:`~pyisyox.Controller` wraps them.
* Event pipeline — :class:`~pyisyox.EventDispatcher`,
  :class:`~pyisyox.WebSocketEventStream`,
  :class:`~pyisyox.Event` and the lifecycle / program-status event
  types.
* Schema — :class:`~pyisyox.Profile` and the typed nodedef / editor /
  command / linkdef dataclasses under :mod:`pyisyox.schema`.
* Classifier — :func:`~pyisyox.classify` for nodedef → HA-platform
  fallback classification.

Controller
----------

.. autoclass:: pyisyox.Controller
    :no-index:
    :members:
    :show-inheritance:

.. autoexception:: pyisyox.ControllerNotConnectedError
    :no-index:

Runtime objects
---------------

.. autoclass:: pyisyox.Node
    :no-index:
    :members:
    :show-inheritance:

.. autoexception:: pyisyox.NodeCommandError
    :no-index:

.. autoclass:: pyisyox.Group
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.Folder
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.Program
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.ProgramFolder
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.ProgramCommand
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.Variable
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.NetworkResource
    :no-index:
    :members:
    :show-inheritance:

Authentication
--------------

.. autoclass:: pyisyox.Auth
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.PortalAuth
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.LocalAuth
    :no-index:
    :members:
    :show-inheritance:

.. autoexception:: pyisyox.AuthError
    :no-index:

HTTP client and load records
----------------------------

.. autoclass:: pyisyox.IoXClient
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.LoadResult
    :no-index:
    :members:

.. autoclass:: pyisyox.ControllerConfig
    :no-index:
    :members:

.. autoclass:: pyisyox.NodeRecord
    :no-index:
    :members:

.. autoclass:: pyisyox.NodePropertyValue
    :no-index:
    :members:

.. autoclass:: pyisyox.GroupRecord
    :no-index:
    :members:

.. autoclass:: pyisyox.FolderRecord
    :no-index:
    :members:

.. autoclass:: pyisyox.ProgramRecord
    :no-index:
    :members:

.. autoclass:: pyisyox.VariableRecord
    :no-index:
    :members:

.. autoclass:: pyisyox.NetworkResourceRecord
    :no-index:
    :members:

Wire-vocabulary enums used in mutation request bodies:

.. autoclass:: pyisyox.NodeType
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.VariableField
    :no-index:
    :members:
    :show-inheritance:

.. autoexception:: pyisyox.ClientError
    :no-index:

.. autoexception:: pyisyox.HTTPError
    :no-index:

Endpoint paths
~~~~~~~~~~~~~~

REST / WebSocket endpoint paths are centralised in
:mod:`pyisyox.paths` — fixed paths as string constants, parametric
paths as ``.format(...)`` templates. Consumers rarely need these
directly (the :class:`~pyisyox.Controller` and
:class:`~pyisyox.IoXClient` use them internally), but they're public
for anyone building against the raw wire surface.

Event pipeline
--------------

.. autoclass:: pyisyox.Event
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.EventDispatcher
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.WebSocketEventStream
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.SystemEventControl
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.TriggerAction
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.NodeLifecycleAction
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.NodeLifecycleEvent
    :no-index:
    :members:
    :show-inheritance:

``pyisyox.NODE_LIFECYCLE_EVENT_INFO_TAGS`` maps each
:class:`~pyisyox.NodeLifecycleAction` verb to the ``<eventInfo>`` child
element names it carries (empty tuple = the frame carries only the node
address); ``pyisyox.DEVICE_WRITE_PROGRESS_EVENT_INFO_TAGS`` does the
same for the ``_7A`` / ``_7M`` device-write progress sub-codes that
ride on ``_7`` (PROGRESS) frames. Both are reference metadata —
pyisyox itself only parses the ``<node>`` element on
:attr:`~pyisyox.NodeLifecycleAction.NODE_ADDED`.

.. autoclass:: pyisyox.ProgramStatusEvent
    :no-index:
    :members:
    :show-inheritance:

The listener type aliases are also exported for typing helpers:

.. autodata:: pyisyox.EventListener
    :no-index:
.. autodata:: pyisyox.NodeLifecycleListener
    :no-index:
.. autodata:: pyisyox.ProgramStatusListener
    :no-index:
.. autodata:: pyisyox.StatusListener
    :no-index:

Schema (profile / nodedefs / editors)
-------------------------------------

.. autoclass:: pyisyox.Profile
    :no-index:
    :members:

.. autoclass:: pyisyox.ProfileMergeResult
    :no-index:
    :members:

See :mod:`pyisyox.schema` for the full schema surface (editors,
commands, linkdefs, UOMs); the most common consumer path is through
``profile.find_nodedef(...)`` / ``profile.find_editor(...)`` —
exercised under the hood by :class:`Node` for command validation.

Classifier
----------

.. autofunction:: pyisyox.classify
    :no-index:

.. autoclass:: pyisyox.ClassificationResult
    :no-index:
    :members:

.. autoclass:: pyisyox.ControllablePlatform
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.ReadingPlatform
    :no-index:
    :members:
    :show-inheritance:

.. autoclass:: pyisyox.Reading
    :no-index:
    :members:

Session helpers and exceptions
------------------------------

.. autofunction:: pyisyox.build_sslcontext
    :no-index:

.. autoexception:: pyisyox.TLSVersionError
    :no-index:

.. autoexception:: pyisyox.ISYConnectionError
    :no-index:
.. autoexception:: pyisyox.ISYInvalidAuthError
    :no-index:
.. autoexception:: pyisyox.ISYMaxConnections
    :no-index:
.. autoexception:: pyisyox.ISYResponseParseError
    :no-index:
.. autoexception:: pyisyox.ISYStreamDataError
    :no-index:
.. autoexception:: pyisyox.ISYStreamDisconnected
    :no-index:
