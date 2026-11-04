"""Authoring entities. Runtime imports of the editor remain optional."""
try:
    from editor.things import Thing, Monster
except ImportError:  # Discovery in the standalone, Qt-free player.
    from plugins.entitybase import Thing
    Monster = Thing


class DeathmatchSettings(Thing):
    pixmap_path = "assets/sprites/logic_timer.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchsettings"
        for key, value in dict(frag_limit=20, time_limit=300.0, respawn_delay=3.0,
                               spawn_protection=1.5, seed=42, show_hud=True).items():
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
                               monster_type="human", disabled=True).items():
            self.properties.setdefault(key, value)


class DeathmatchSpawn(Thing):
    """Feet position of a shared respawn; runtime heading is in degrees."""
    pixmap_path = "assets/sprites/player.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchspawn"
        self.properties.setdefault("angle", 0.0)


class DeathmatchNode(Thing):
    """A navigation waypoint at floor height; links are bidirectional names."""
    pixmap_path = "assets/sprites/logic_relay.png"

    def __init__(self, pos=None, properties=None):
        super().__init__(pos, properties)
        self.properties["type"] = "deathmatchnode"
        self.properties.setdefault("links", "")
