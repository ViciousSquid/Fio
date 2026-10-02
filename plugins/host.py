"""
The plugin **host**: one stable, fail-safe seam between a plugin and the whole
engine.

The rest of the plugin API (entities, I/O, the lifecycle hooks, the runtime
services) is a *curated* surface — deliberately small. The host is the
*open-ended* surface. It exists so the engine can be extended in ways this API
never anticipated **without changing the engine**:

* :class:`EventBus` — the engine emits named events at every meaningful moment
  (``play_start``, ``tick``, ``player_damage``, ``portal_transit``,
  ``pickup_collected`` …). A plugin subscribes to any of them, including events
  that don't exist yet — a subscription to an unknown event is simply dormant
  until something emits it. Adding a brand-new engine signal later is a single
  ``emit()`` call; no plugin-API change, no host change.

* :class:`PluginHost` — a single object handed to each plugin that reaches the
  live engine. Named accessors cover the common subsystems; :meth:`PluginHost.get`
  and attribute pass-through reach *anything else the host object exposes*,
  including subsystems added in the future. A cross-plugin service registry
  (:meth:`PluginHost.provide` / :meth:`PluginHost.service`) and a guarded
  monkey-patch helper (:meth:`PluginHost.wrap`) let plugins add and share
  behaviour.

Everything here is defensive: a handler that raises is logged and skipped, it
never takes down a tick or another plugin. The host holds only weak, read-through
references to engine objects — it adds no state the engine has to know about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@dataclass
class Event:
    """A single dispatched event.

    Handlers receive one of these. Payload fields are reachable three ways —
    ``ev.data['player']``, ``ev.get('player')`` and ``ev.player`` — so handlers
    stay terse. ``ev.logic`` is always present (the play session's logic/host
    object the engine passed). Call :meth:`stop` to halt propagation to
    lower-priority handlers.
    """

    name: str
    data: Dict[str, Any] = field(default_factory=dict)
    _stopped: bool = field(default=False, repr=False)

    def get(self, key, default=None):
        return self.data.get(key, default)

    def __getitem__(self, key):
        return self.data.get(key)

    def __getattr__(self, key):
        # Only reached when normal attribute lookup fails, so real fields
        # (name/data) are never shadowed. Lets ``ev.player`` read the payload.
        try:
            return self.data[key]
        except KeyError:
            raise AttributeError(key)

    def stop(self):
        """Stop this event reaching any remaining (lower-priority) handlers."""
        self._stopped = True


class EventBus:
    """A tiny, fail-safe publish/subscribe bus.

    Handlers are called highest-priority first; ties keep subscription order.
    A handler that raises is logged and skipped. :meth:`emit` early-outs when an
    event has no subscribers, so instrumenting the engine with emit points that
    nobody listens to costs almost nothing.
    """

    def __init__(self, log: Optional[Callable[[str], None]] = None):
        # event name -> list of (priority, sequence, handler)
        self._subs: Dict[str, List[Tuple[int, int, Callable]]] = {}
        self._seq = 0
        self._log = log or (lambda _m: None)
        # Bumped whenever the subscription set changes, so a hot-path caller
        # (the engine's per-frame tick gate) can tell "did anything change?"
        # with a single int compare instead of re-scanning.
        self.gen = 0

    def on(self, event: str, handler: Callable, priority: int = 0) -> Callable:
        """Subscribe *handler* to *event*. Returns *handler* (usable as a decorator)."""
        self._seq += 1
        self.gen += 1
        bucket = self._subs.setdefault(str(event), [])
        bucket.append((priority, self._seq, handler))
        # Higher priority first; stable within a priority via the sequence.
        bucket.sort(key=lambda t: (-t[0], t[1]))
        return handler

    def off(self, event: str, handler: Callable) -> None:
        """Unsubscribe *handler* from *event* (no-op if it wasn't subscribed)."""
        bucket = self._subs.get(str(event))
        if bucket:
            self.gen += 1
            self._subs[str(event)] = [t for t in bucket if t[2] is not handler]

    def has(self, event: str) -> bool:
        """True if *event* has at least one subscriber."""
        return bool(self._subs.get(str(event)))

    def emit(self, event: str, **data) -> Optional[Event]:
        """Dispatch *event* with keyword payload to its subscribers.

        Returns the :class:`Event` (so a caller can read fields a handler set on
        ``ev.data``), or ``None`` if nobody was listening.
        """
        bucket = self._subs.get(str(event))
        if not bucket:
            return None
        ev = Event(str(event), dict(data))
        for _pri, _seq, handler in list(bucket):
            if ev._stopped:
                break
            try:
                handler(ev)
            except Exception:
                import traceback
                self._log(f"event handler for '{event}' failed:\n{traceback.format_exc()}")
        return ev

    def clear(self) -> None:
        self.gen += 1
        self._subs.clear()


# ---------------------------------------------------------------------------
# Host
# ---------------------------------------------------------------------------

class PluginHost:
    """A plugin's window into the whole engine — created once per plugin.

    Wraps the live *target* (the engine's ``LogicThread`` in the editor, or the
    player's logic bridge). The named properties below are conveniences over the
    subsystems the target actually holds; when a subsystem isn't present on this
    host they return ``None`` rather than raising. For anything without a named
    accessor — including parts added later — use :meth:`get` (dotted lookup) or
    just read the attribute straight off the host: unknown attributes fall
    through to the target.
    """

    def __init__(self, manager, target, plugin, kind: str = "engine"):
        self._manager = manager
        self._target = target
        self._plugin = plugin
        #: ``"engine"`` (editor play mode) or ``"player"`` (standalone player).
        self.kind = kind

    # -- the live objects ---------------------------------------------------
    @property
    def logic(self):
        """The play session's logic object (a.k.a. the engine host target)."""
        return self._target

    #: Alias — the same object, named for readers who think "the engine".
    @property
    def engine(self):
        return self._target

    @property
    def scene(self):
        """The live list of scene entities (``things``).

        Returns the *actual* list (so mutating it affects the scene), or ``[]``
        only when the host exposes no ``things`` at all — never a throwaway copy
        of a merely-empty scene.
        """
        things = getattr(self._target, "things", None)
        return things if things is not None else []

    @property
    def player(self):
        return getattr(self._target, "player", None)

    @property
    def io(self):
        """The play session's ``IOManager`` (fire outputs / register inputs)."""
        return getattr(self._target, "io_manager", None)

    @property
    def globals(self):
        """The cross-level :class:`~plugins.api.GlobalStore`."""
        return self._manager.global_store

    @property
    def game_state(self):
        return getattr(self._target, "game_state", None)

    @property
    def monster_ai(self):
        return getattr(self._target, "monster_ai", None)

    @property
    def terrain(self):
        return getattr(self._target, "terrain", None)

    @property
    def editor(self):
        """The editor main window if running inside the editor, else ``None``."""
        try:
            from editor.main_window import MainWindow  # noqa: F401
        except Exception:
            return None
        return getattr(self._target, "main_window", None) or \
            getattr(getattr(self._target, "editor_state", None), "main_window", None)

    # -- generic reach ------------------------------------------------------
    def get(self, path: str, default=None):
        """Dotted attribute lookup from the host target.

        ``host.get('game_state')`` or ``host.get('editor_state.things')``.
        Returns *default* if any step is missing — so probing for a subsystem
        that may not exist on this host is safe.
        """
        obj = self._target
        for part in str(path).split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return default
        return obj

    def __getattr__(self, name):
        # Reached only when a real attribute/property lookup misses, so the
        # named accessors above always win. Everything else the target exposes
        # is reachable as ``host.<whatever>`` — this is the "touch any part of
        # the engine, including parts added later" escape hatch.
        target = self.__dict__.get("_target")
        if target is not None:
            try:
                return getattr(target, name)
            except AttributeError:
                pass
        raise AttributeError(name)

    # -- events -------------------------------------------------------------
    @property
    def events(self) -> EventBus:
        """The raw, process-wide :class:`EventBus` (advanced use)."""
        return self._manager.events

    def on(self, event: str, handler: Callable, priority: int = 0) -> Callable:
        """Subscribe to an engine (or plugin) *event*.

        The handler is **gated on this plugin's live ``enabled`` flag** — it runs
        only while the plugin is on — mirroring how I/O input handlers behave, so
        a disabled plugin's event hooks go quiet without unsubscribing.
        """
        plugin = self._plugin

        def gated(ev):
            if getattr(plugin, "enabled", True):
                return handler(ev)

        # Remember the wrapper so off() can find it by the original handler.
        self._gated = getattr(self, "_gated", {})
        self._gated[handler] = gated
        return self._manager.events.on(event, gated, priority)

    def off(self, event: str, handler: Callable) -> None:
        gated = getattr(self, "_gated", {}).get(handler, handler)
        self._manager.events.off(event, gated)

    def emit(self, event: str, **data):
        """Emit a custom event (e.g. for other plugins to observe)."""
        return self._manager.events.emit(event, **data)

    # -- cross-plugin services & extension ---------------------------------
    def provide(self, name: str, value) -> None:
        """Publish a named service other plugins can look up via :meth:`service`."""
        self._manager.services[str(name)] = value

    def service(self, name: str, default=None):
        """Look up a service another plugin :meth:`provide`-d, or *default*."""
        return self._manager.services.get(str(name), default)

    def wrap(self, obj, method_name: str, wrapper: Callable) -> bool:
        """Wrap ``obj.method_name`` with ``wrapper(original, *args, **kwargs)``.

        A guarded monkey-patch helper for the rare extension that must intercept
        an existing engine call rather than react to an event. If the wrapper
        raises, the original is called so behaviour degrades to stock. Returns
        True if the wrap was installed.
        """
        original = getattr(obj, method_name, None)
        if not callable(original):
            return False

        def wrapped(*args, **kwargs):
            try:
                return wrapper(original, *args, **kwargs)
            except Exception:
                import traceback
                self._manager._log(
                    f"[{self._plugin.name}] wrap of {method_name} failed, "
                    f"using original:\n{traceback.format_exc()}")
                return original(*args, **kwargs)

        try:
            setattr(obj, method_name, wrapped)
            return True
        except Exception:
            return False

    def register_renderer(self, name: str, cls) -> bool:
        """Register a swappable renderer class under *name* (see the editor API).

        Returns True if registered, False in a host with no viewport (player).
        """
        try:
            from engine.qt_game_view import register_renderer
        except Exception:
            return False
        return register_renderer(name, cls)

    def log(self, message: str) -> None:
        self._manager._debug(f"[{self._plugin.name}] {message}")

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"<PluginHost kind={self.kind} plugin={self._plugin.name}>"
