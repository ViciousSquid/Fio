"""
The Tidy plugin: build "put everything away" games (books back on the shelf,
tidy up the museum, sort the warehouse) where the player picks up many objects
and stows them in receptacles until a goal is met.

This module wires the pieces together:

* declares the three entity types and their I/O (``register``),
* registers runtime I/O input handlers (``register_runtime``),
* drives the carry/place session across the play lifecycle
  (``on_play_start`` / ``on_tick`` / ``on_play_stop``).

The actual mechanics live in :mod:`plugins.tidy.runtime`; the entities in
:mod:`plugins.tidy.entities`.
"""

from __future__ import annotations

from plugins.api import FioPlugin, TickContext, io_def

from .entities import TidyGoal, TidyObject, TidyReceptacle
from .runtime import TidySession


class TidyPlugin(FioPlugin):
    name = "tidy"
    version = "1.0.0"
    description = "Pick-up-and-put-away gameplay for large object-tidying games."
    category = "Tidy"
    # Off by default: this plugin only matters for maps built around its
    # entities, so it stays inert until a level that references its data is
    # loaded. The manager auto-enables it then (see auto_enable_for_map), so a
    # tidy map plays without the user hunting through the Plugins menu, while
    # ordinary maps never pay for gameplay they don't use.
    enabled = False

    # -- load time ----------------------------------------------------------
    def register(self, api):
        api.register_entity(TidyObject, menu_label="Tidy Object")
        api.register_entity(TidyReceptacle, menu_label="Tidy Receptacle (shelf/bin)")
        api.register_entity(TidyGoal, menu_label="Tidy Goal")

        api.register_io(
            'tidyobject',
            inputs=[
                io_def('Reset', "Return this object to where it started"),
                io_def('Enable', "Make it pickable again"),
                io_def('Disable', "Make it un-pickable / hidden from tidying"),
            ],
            outputs=[
                io_def('OnPickedUp', "Fired when the player picks this up"),
                io_def('OnDropped', "Fired when the player drops it without stowing"),
                io_def('OnTidied', "Fired when this object is stowed in a receptacle"),
            ],
        )
        api.register_io(
            'tidyreceptacle',
            inputs=[
                io_def('Reset', "Empty the receptacle; send its objects home"),
                io_def('Enable', "Allow objects to be placed here"),
                io_def('Disable', "Refuse new objects"),
            ],
            outputs=[
                io_def('OnObjectPlaced', "Fired each time an object is stowed here",
                       "int"),
                io_def('OnFull', "Fired when the receptacle reaches capacity"),
            ],
        )
        api.register_io(
            'tidygoal',
            inputs=[
                io_def('Enable', "Count toward completion"),
                io_def('Disable', "Stop counting"),
            ],
            outputs=[
                io_def('OnProgress', "Fired on every stow: 'done/need'", "string"),
                io_def('OnComplete', "Fired once when the tidy target is reached"),
            ],
        )

    # -- runtime attach -----------------------------------------------------
    def register_runtime(self, api):
        def _session(logic):
            return getattr(logic, '_tidy', None)

        def obj_reset(entity, param, logic):
            s = _session(logic)
            if s:
                s.reset_object(entity)

        def obj_enable(entity, param, logic):
            entity.properties['disabled'] = False
            s = _session(logic)
            if s and entity in s.objects:
                s.grid.remove(entity)
                if not entity.properties.get('tidied'):
                    s.grid.add(entity)

        def obj_disable(entity, param, logic):
            entity.properties['disabled'] = True
            s = _session(logic)
            if s:
                s.grid.remove(entity)
                s._falling.pop(id(entity), None)   # a disabled object stops falling

        def recept_reset(entity, param, logic):
            s = _session(logic)
            if not s:
                return
            ids = list(s._fill.get(id(entity), ()))
            id_to_obj = {id(o): o for o in s.objects}
            for oid in ids:
                obj = id_to_obj.get(oid)
                if obj is not None:
                    s.reset_object(obj)
            s._fill.pop(id(entity), None)
            s._full_fired.discard(id(entity))

        def recept_enable(entity, param, logic):
            entity.properties['disabled'] = False

        def recept_disable(entity, param, logic):
            entity.properties['disabled'] = True

        def goal_enable(entity, param, logic):
            entity.properties['disabled'] = False

        def goal_disable(entity, param, logic):
            entity.properties['disabled'] = True

        api.register_input_handler('tidyobject', 'reset', obj_reset)
        api.register_input_handler('tidyobject', 'enable', obj_enable)
        api.register_input_handler('tidyobject', 'disable', obj_disable)
        api.register_input_handler('tidyreceptacle', 'reset', recept_reset)
        api.register_input_handler('tidyreceptacle', 'enable', recept_enable)
        api.register_input_handler('tidyreceptacle', 'disable', recept_disable)
        api.register_input_handler('tidygoal', 'enable', goal_enable)
        api.register_input_handler('tidygoal', 'disable', goal_disable)

    # -- lifecycle ----------------------------------------------------------
    def on_play_start(self, logic):
        session = TidySession(logic)
        session.start()
        logic._tidy = session

    def on_play_stop(self, logic):
        session = getattr(logic, '_tidy', None)
        if session is not None:
            session.stop()
            logic._tidy = None

    def on_tick(self, logic, ctx: TickContext):
        session = getattr(logic, '_tidy', None)
        if session is None:
            return
        session.tick(ctx)
        # When nothing else claimed the HUD line this tick, show live progress.
        if not getattr(logic, 'current_hud_message', ''):
            line = session.hud_line()
            if line:
                logic.current_hud_message = line
