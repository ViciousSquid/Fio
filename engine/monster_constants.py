"""
Monster AI constants
All distances are in world units. All times are in seconds.
"""

# How far away (units) a monster can detect the player
MONSTER_SIGHT_RANGE = 1024

# Default team assignment for monsters (empty = no team / neutral)
MONSTER_DEFAULT_TEAM = ""

# Seconds between each attack (per-monster cooldown)
MONSTER_SHOOT_INTERVAL = 1.5

# How long the shoot sprite is displayed after firing before reverting to idle
MONSTER_SHOOT_ANIM_TIME = 0.35


# World-units per second that a monster moves toward the player
MONSTER_MOVE_SPEED = 90.0

# Monster stops moving when it gets this close (to avoid clipping into player)
MONSTER_STOP_DISTANCE = 85.0

# ---------------------------------------------------------------------------
# Sprite frame filenames (relative to assets/sprites/monsters/<type>/)
# ---------------------------------------------------------------------------

MONSTER_SPRITE_IDLE  = "idle.png"
MONSTER_SPRITE_SHOOT = "shoot.png"
MONSTER_SPRITE_DEAD  = "dead.png"

MONSTER_PROJECTILE_SPRITE = "projectile.png"

# ---------------------------------------------------------------------------
# Billboard size (world units) — used by the 3D renderer for each subtype.
# These are the defaults; individual monsters can override them via the
# Customise Sprites dialog (stored as sprite_width / sprite_height in their
# properties dict).
# ---------------------------------------------------------------------------

MONSTER_SPRITE_SIZES = {
    'human':  (128, 192),   # width, height — upright humanoid enemy
    'flying': (160, 160),    # wider, shorter — airborne creature
}

# Fallback used when monster_type is not listed above
MONSTER_SPRITE_SIZE_DEFAULT = (128, 128)

# ---------------------------------------------------------------------------
# Monster variants — per-type list of alternate sprite folder names.
#
# When a monster's 'variant' property is set to one of these values, the
# sprite path changes from:
#   assets/sprites/monsters/<monster_type>/<frame>.png
# to:
#   assets/sprites/monsters/<monster_type>/<variant>/<frame>.png
#
# The special value '<None>' (the default) means no variant — use the
# base folder.  Add more entries per type as new variant art is created.
# ---------------------------------------------------------------------------

MONSTER_VARIANTS = {
    'human':  ['variant1'],
    'flying': ['variant1'],
}

# ---------------------------------------------------------------------------
# Projectile constants  (flying monster ranged attack)
# ---------------------------------------------------------------------------
MONSTER_PROJECTILE_SPEED    = 512.0   # world-units / second
MONSTER_PROJECTILE_MAX_DIST = 2048  # despawn after travelling this far
MONSTER_PROJECTILE_SPRITE_SIZE = (40.0, 40.0)   # billboard size in world units

# ---------------------------------------------------------------------------
# Monster physics & collision
# ---------------------------------------------------------------------------
MONSTER_GRAVITY        = -500.0   # same gravity as player
MONSTER_TERMINAL_VEL   = -500.0
MONSTER_MIN_WIDTH      = 200.0    # monsters are always at least 200px wide
MONSTER_WALL_MARGIN    = 100.0    # half of MONSTER_MIN_WIDTH — keep this far from wall surfaces
MONSTER_DEAD_FALL_SPEED = 300.0   # world-units/sec the dead sprite falls

# ---------------------------------------------------------------------------
# Patrol obstacle avoidance
# ---------------------------------------------------------------------------
# When a monster is blocked by walls for this many consecutive ticks,
# it will search for a nearby PathNode to detour around the obstacle.
MONSTER_STUCK_THRESHOLD  = 30    # ticks (~0.5s at 60fps) before detour kicks in
# Maximum distance (world units) to search for a detour node
MONSTER_DETOUR_RANGE     = 1024.0

# ---------------------------------------------------------------------------
# Weapon damage per gun type
# ---------------------------------------------------------------------------
# Non-firing weapons (see NON_FIRING_WEAPONS below) are deliberately omitted —
# they deal no damage.
WEAPON_DAMAGE = {
    'gun1': 25,
    'gun2': 75,    # shotgun — 3× gun1
}

# ---------------------------------------------------------------------------
# Per-weapon shoot sound
# ---------------------------------------------------------------------------
# Non-firing weapons (see NON_FIRING_WEAPONS below) are deliberately omitted —
# they play no shoot sound.
WEAPON_SHOOT_SOUND = {
    'gun1': 'shoot.wav',
    'gun2': 'shoot2.wav',
}

# ---------------------------------------------------------------------------
# Non-firing weapons
# ---------------------------------------------------------------------------
# Display-only / cosmetic guns: they can be picked up and shown in the HUD, but
# firing them does nothing — no hitscan or projectile, no muzzle-flash
# animation, and no sound.  These weapons have no HUD_flash sprite and are
# intentionally absent from WEAPON_DAMAGE and WEAPON_SHOOT_SOUND above.
NON_FIRING_WEAPONS = {'cig'}


# ---------------------------------------------------------------------------
# Per-monster-type shoot sounds (used by MonsterAI)
# ---------------------------------------------------------------------------
MONSTER_SHOOT_SOUNDS = {
    'human':  'shoot.wav',
    'flying': 'shoot_flying.wav',
}
MONSTER_SHOOT_SOUND_DEFAULT = 'shoot.wav'


# ---------------------------------------------------------------------------
# Team-based combat
# ---------------------------------------------------------------------------
# Monsters on different teams are enemies and will always attack one another
# first before targeting the player.  Same-team monsters never damage each
# other via crossfire.  The 'team' property is a string (e.g. "1", "2",
# "player") — leave empty for neutral / no-team behaviour.

# ---------------------------------------------------------------------------
# Flying monster bite attack (melee when very close)
# ---------------------------------------------------------------------------
# When a flying monster gets within this distance of its target, it switches
# from projectile shooting to a bite attack that deals double damage.
MONSTER_BITE_DISTANCE = 80.0    # world units — must be < MONSTER_STOP_DISTANCE
MONSTER_BITE_DAMAGE_MULT = 2.0  # bite deals 2× normal damage
