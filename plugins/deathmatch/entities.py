"""Authoring entities. Runtime imports of the editor remain optional."""
try:
    from editor.things import Thing, Monster
except ImportError:  # Discovery in the standalone, Qt-free player.
    from plugins.entitybase import Thing
    Monster = Thing
from pathlib import Path
from engine.prop_entity import Prop


class DeathmatchSettings(Thing):
    pixmap_path = "assets/sprites/logic_timer.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchsettings"
        for key, value in dict(frag_limit=20, time_limit=300.0, respawn_delay=3.0,
                               spawn_protection=1.5, seed=42, show_hud=True,
                               game_type="deathmatch", player_team="red", friendly_fire=False,
                               capture_limit=3, flag_return_time=40.0, team_skins=True).items():
            self.properties.setdefault(key, value)


class DeathmatchBot(Monster):
    """A native shootable Monster, driven by the plugin instead of stock AI."""
    pixmap_path = "assets/sprites/monsters/human/idle.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchbot"
        for key, value in dict(skill="normal", max_health=100, health=100,
                               vision_range=1800.0, hearing_range=1000.0,
                               field_of_view=160.0, preferred_range=480.0,
                               sprite_width=64.0, sprite_height=128.0,
                               monster_type="human", disabled=True, team="auto").items():
            self.properties.setdefault(key, value)


class DeathmatchSpawn(Thing):
    """Feet position of a shared respawn; runtime heading is in degrees."""
    pixmap_path = "assets/sprites/player.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchspawn"
        self.properties.setdefault("angle", 0.0)
        self.properties.setdefault("team", "any")


class DeathmatchNode(Thing):
    """A navigation area; marker height is independent of the walking floor."""
    pixmap_path = "assets/sprites/logic_relay.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchnode"
        self.properties.setdefault("links", "")
        self.properties.setdefault("radius", 36.0)
        self.properties.setdefault("show_radius", False)

    def get_radius(self):
        from .navigation import Waypoint
        return Waypoint(self.pos, self.properties.get("radius", 36.0)).radius


class DeathmatchFlag(Prop):
    """A visible flag, placed at floor height; the mode owns its state."""
    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchflag"
        team = self.properties.setdefault("team", "red")
        sprite = str(Path(__file__).parent / "sprites" / ("blue_flag.png" if team == "blue" else "red_flag.png"))
        self.properties.update(render_mode="billboard", sprite_path=sprite, sprite_size=[80, 128],
                               collect_enabled=False, carry_enabled=False, no_collision=True)
