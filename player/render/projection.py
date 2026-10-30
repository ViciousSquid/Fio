"""Read desktop entity projections rather than inventing mobile entity rules."""
import time
from engine.entity_table import EntityTable
from engine.shaders import MAX_LIGHTS


class EntityProjection:
    def __init__(self):
        self.table = EntityTable()

    def update(self, world):
        self.table.begin_frame(world.state.things, effect_runtime=True,
                              effect_store=world.logic.session_runtime.effect_store)
        return self.table


MAX_LIGHT_DISTANCE = 4096.0


def lights(table, eye, limit=MAX_LIGHTS, *, player_position=None, cutscene=False,
           visible_horizon=MAX_LIGHT_DISTANCE):
    # All light families, including Effect flicker/explosion flashes, use the
    # authoritative engine projection. Select nearby influence only at the cap.
    center = eye if cutscene or player_position is None else player_position
    def in_range(row):
        radius = MAX_LIGHT_DISTANCE
        if cutscene:
            # A light outside the camera's radius may still illuminate a wall
            # inside it. Retire it only after its entire influence is hidden
            # beyond opaque fog (or the camera's visible clip volume).
            radius = max(radius, visible_horizon + float(table.light_params[row, 1]))
        return sum((float(table.pos[row, a])-float(center[a]))**2 for a in range(3)) <= radius**2
    candidates = [int(i) for i in table.light_slots
                  if not table.hidden[i] and table.light_enabled[i] and (table.light_params[i, 0] > 0 or table.light_shadow_only[i])
                  and in_range(i)]
    if len(candidates) > limit:
        candidates.sort(key=lambda i: sum((table.pos[i, a]-eye[a])**2 for a in range(3)) /
                        max(1, float(table.light_params[i, 1]))**2)
    return [dict(pos=table.pos[i], color=table.light_color[i],
                 intensity=float(table.light_params[i, 0]), radius=float(table.light_params[i, 1]),
                 casts_shadows=bool(table.light_casts_shadows[i]),
                  shadow_only=bool(table.light_shadow_only[i]),
                  shadow_strength=float(table.light_shadow_strength[i])) for i in candidates[:limit]]


def effect_image(table, row):
    from engine.effect_entity import EFFECT_FIRE_TEXTURES, EFFECT_ORB_TEXTURES
    family = int(table.effect_type[row])
    variant = int(table.effect_fire_variant[row])
    if family == 0:
        path = EFFECT_FIRE_TEXTURES[variant]
    elif family == 2:
        path = EFFECT_ORB_TEXTURES[variant]
    elif family == 3:
        path = table.effect_custom_path(int(table.effect_custom_id[row]))
    else:
        path = 'assets/textures/effects/explosion.png'
    return path, max(0, time.perf_counter()-float(table.effect_spawn_time[row])), float(table.effect_phase[row])
