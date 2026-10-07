# View Filters

The **Filter** menu shows and hides whole kinds of object in the editor, in the
manner of GtkRadiant's filters. Each entry is ticked while that kind shows;
**Show All** brings everything back.

| Filter | What it covers |
|--------|----------------|
| World brushes | ordinary brushes (glow brushes included) |
| Movers & doors | brushes marked as a mover or a door |
| Triggers | trigger brushes and trigger entities |
| Water, Glass, Fog | brushes with the Water, Glass or Fog shader |
| Terrain | the terrain |
| Lights | light entities — the scene stays lit, as in Radiant |
| Path nodes | PathNodes, **and their connection lines** (node chains, monster patrol lines, teleport targets) |
| Monsters | monsters |
| Props & models | props and anything drawn as a model |
| Effects | Effects (FIRE, ORB, EXPLOSION, CUSTOM) |
| Portals | portals, their outlines and their link lines |
| Logic entities | relays, gates, timers, commands, cameras, spawners, states, level changers |
| Speakers | speakers |
| Player starts | player starts |
| Other entities | anything else, plugin entities included |

A filtered object is hidden **everywhere in the editor**: the 3D view, the 2D
views, picking (it can't be clicked or box-selected), and every I/O or
connection line that starts or ends on it. Filters are view state: they are
never saved with the map, they persist while you load other maps, and **Play
ignores them** — the game always shows the world as it is.

## How it works

The groups are read from the same class bits the
[RenderTable](https://github.com/ViciousSquid/Fio/wiki/RenderTable) and
[EntityTable](https://github.com/ViciousSquid/Fio/wiki/EntityTable) carry
(`engine/view_filters.py`), so a filter can never disagree with what the renderer
was told an object is.

Filtering happens where the logic thread publishes a frame: filtered brush rows are
left out of `visible_brush_slots` and `all_brush_slots`, and filtered entity rows
out of `visible_thing_slots`. The renderer is not involved — whichever renderer is
active (see the [Renderer Development Guide](https://github.com/ViciousSquid/Fio/wiki/Renderer-Development-Guide))
simply receives fewer rows. You can watch it happen in
[Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables): the visible
slot counts drop as you untick a filter.
