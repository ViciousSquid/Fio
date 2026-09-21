import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Tuple, Type


#: Version of the plugin API surface this module implements. Compare against
#: :attr:`FioPlugin.api_version` (see :func:`version_tuple`).
#:
#: * 1.2.0 — the open-ended extension surface: :class:`plugins.host.PluginHost`,
#:   the engine event bus, and the ``connect(host)`` hook.
#: * 1.3.0 — render hooks, swappable-renderer registration, and editor-UI extensions.
#: * 1.4.0 — optional editor Tools actions and console-command registration.
API_VERSION = "1.4.0"
API_VERSION_INFO = (1, 4, 0)


def version_tuple(value: str) -> tuple:
    """Parse a dotted version string into a comparable int tuple.