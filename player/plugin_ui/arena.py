"""Reach the installed Deathmatch plugin for the world being played.

Every action goes through a seam desktop Fio's own UI uses, so a rule is
never reimplemented here:

* match commands go through the plugin manager's console dispatch, exactly
  what desktop's debug console calls (``addbot``, ``removebot``,
  ``fraglimit``, ``dflaglight``, ``spectate``);
* restarting runs the plugin's own **Restart Deathmatch** pause-menu item;
* the HUD reads the immutable ``snapshot`` the plugin publishes for its
  desktop scoreboard painter.

Commands run on the app thread between simulation ticks -- the player owns a
single simulation thread -- under the same locks the desktop console takes.
"""
from __future__ import annotations

import functools

PLUGIN = 'deathmatch'
RESTART_ITEM = 'Restart Deathmatch'
SKILLS = ('easy', 'normal', 'hard')
TEAMS = ('auto', 'red', 'blue')
#: Game types in the order desktop's LAN lobby offers them.
GAME_TYPES = (('deathmatch', 'Deathmatch'), ('ctf', 'Capture the Flag'),
              ('teamdeathmatch', 'Team Deathmatch'))


def manager(world):
    return getattr(getattr(world, 'logic', None), 'plugins', None) if world else None


def plugin(world=None, plugin_manager=None):
    """The installed, enabled Deathmatch plugin, or None."""
    plugin_manager = plugin_manager or manager(world)
    if plugin_manager is None:
        return None
    found = plugin_manager.find_plugin(PLUGIN)
    return found if found is not None and plugin_manager.is_enabled(found) else None


@functools.lru_cache(maxsize=1)
def installed():
    """Whether the Deathmatch plugin ships with this player at all."""
    try:
        from plugins.manager import get_manager, load_plugins
        load_plugins()
        return get_manager().find_plugin(PLUGIN) is not None
    except Exception:
        return False


def session(world):
    """The running arena session for *world*, or None (ordinary worlds)."""
    found = plugin(world)
    if found is None:
        return None
    running = getattr(found, '_sessions', {}).get(world.logic)
    return running if running is not None and getattr(running, '_started', False) else None


def snapshot(world):
    """The plugin's latest published scoreboard state ({} outside arenas)."""
    running = session(world)
    return dict(running.snapshot) if running is not None and running.snapshot else {}


def is_client(world):
    running = session(world)
    return bool(running is not None and running.network is not None and running.network.is_client)


def is_host(world):
    running = session(world)
    return bool(running is not None and running.network is not None and not running.network.is_client)


def command(world, name, args=''):
    """Run one of the plugin's console commands; return its reply."""
    plugin_manager = manager(world)
    if plugin_manager is None or plugin(world) is None:
        return 'The Deathmatch plugin is not enabled for this world.'
    handled, reply = plugin_manager.dispatch_console_command(
        name, args, logic=world.logic, main_window=None, play_mode=True)
    if not handled:
        return f'{name} is unavailable.'
    return str(reply) if reply else ''


def restart(world):
    """Run the plugin's own Restart Deathmatch pause-menu item."""
    if session(world) is None:
        return 'Start a Deathmatch arena first.'
    if is_client(world):
        return 'The LAN host controls restarts.'
    plugin_manager = manager(world)
    for label, callback, _close in plugin_manager.pause_menu_items():
        if label == RESTART_ITEM:
            plugin_manager.run_pause_menu_item(callback, None, world.logic)
            return 'Restarting the match.'
    return 'Restart is unavailable.'


def can_restart(world):
    """True when the match has ended and this device may start the next one."""
    state = snapshot(world)
    return bool(state.get('finished')) and not state.get('network_client')


def add_bot(world, skill='normal', team='auto'):
    if skill not in SKILLS or team not in TEAMS:
        raise ValueError('unknown bot skill or team')
    return command(world, 'addbot', f'{skill} {team}')


def remove_bot(world):
    return command(world, 'removebot')


def frag_limit(world, value):
    return command(world, 'fraglimit', str(max(0, min(10000, int(value)))))


def flag_shadows(world, enabled):
    return command(world, 'dflaglight', 'on' if enabled else 'off')


def spectate(world, choice):
    """``free``, ``off``, ``next``, ``prev`` or a participant's name."""
    choice = str(choice).strip()
    if not choice:
        raise ValueError('choose a spectator view')
    # The plugin compares the whole argument with participant names.
    return command(world, 'spectate', choice)


def watchable(world):
    """``[(key, name, team, alive, spectator)]`` for the spectator menu."""
    return list(snapshot(world).get('watchable', ()))


def flag_shadows_enabled(world):
    running = session(world)
    return bool(running is not None and getattr(running.game, 'flag_shadows', False))


def spectator_glasses(world):
    """``[(eye position, style)]`` of other spectators, for the renderer.

    Desktop draws these from the render state its renderer publishes; the
    player draws them from the same session data.
    """
    running = session(world)
    if running is None:
        return []
    local = running.fighters.get('player')
    return [(running.eye(fighter), fighter.glasses_style)
            for fighter in running.fighters.values()
            if fighter.spectator and fighter is not local]
