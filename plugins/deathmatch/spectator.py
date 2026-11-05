"""Non-participating cameras, with host-authoritative LAN membership."""
import math


class Spectators:
    def __init__(self, session):
        self.session = session
        self.original_physics = session.fighters['player'].actor.physics_enabled
        self.original_camera = session.logic.camera.camera_mode

    @property
    def local(self):
        return self.session.fighters['player']

    def install(self):
        s = self.session
        s._hook(s.logic.player_runtime, 'update_primary', self.primary_input)
        for owner, name in ((s.logic.interaction_runtime, 'handle'),
                            (s.logic.trigger_runtime, '_handle_triggers')):
            s._hook(owner, name, self.player_action)
        s._hook(s.logic.prop_runtime, '_collect', self.collect)
        physics = s.logic.session_runtime.physics_world
        if physics is not None:
            s._hook(physics, 'step', self.physics_step)
        s._hook(s.logic.render_runtime, 'prepare_render_state', self.render)

    def player_action(self, original, *args, **kwargs):
        if not self.local.spectator:
            return original(*args, **kwargs)

    def collect(self, original, prop, **kwargs):
        # Bot grants remain valid while the local player is watching.
        if self.local.spectator and kwargs.get('grant') is None:
            return False
        return original(prop, **kwargs)

    def physics_step(self, original, delta, player=None):
        return original(delta, None if self.local.spectator else player)

    def choose(self, enabled=True, target=None):
        s = self.session
        if target is not None:
            other = s.fighters.get(target)
            if other is None or other is self.local or other.spectator or not other.alive:
                return 'Choose a living participant to follow.'
        if s.network is not None and s.network.is_client:
            server_target = 'player' if target == 'host' else target
            s.network.pending_spectate = bool(enabled)
            s.network.transport.send(dict(type='spectate', enabled=bool(enabled), target=server_target,
                glasses=s.logic.player_runtime.glasses_style))
        self.set(self.local, enabled, target)
        s.publish()
        return 'Joined the match.' if not enabled else 'Spectating: free camera.' if target is None else f'Watching {s.fighters[target].name}.'

    def set(self, fighter, enabled, target=None):
        s, enabled = self.session, bool(enabled)
        changed = fighter.spectator != enabled
        if fighter is self.local and changed and enabled:
            self.original_physics = fighter.actor.physics_enabled
            self.original_camera = s.logic.camera.camera_mode
        fighter.spectator, fighter.spectator_target = enabled, target if enabled else None
        client = s.network is not None and s.network.is_client
        if enabled:
            if changed and not client:
                s.game.drop(fighter)
            fighter.alive = False
            fighter.target, fighter.route, fighter.traversal = None, [], None
            fighter.shoot_until = 0
            fighter.actor.physics_enabled = False
            fighter.actor.on_ground, fighter.actor.ground_object = False, None
            fighter.actor.set_ducked(False)
            fighter.actor.view_offset = 0
            if fighter.entity is not None:
                fighter.entity.properties.update(dead=True, hidden=True, is_shooting=False)
            fighter.mode = 'spectator'
        elif changed:
            fighter.actor.physics_enabled = True
            if not client:
                s.respawn(fighter)
        if changed and fighter.entity is not None:
            from engine.change_journal import touch
            touch(fighter.entity)
        if fighter is self.local:
            rt = s.logic.player_runtime
            rt.player_dead = False
            s.logic.camera.camera_mode = 'First Person' if enabled else self.original_camera
            s.logic.camera.camera_transition = None
            if not enabled:
                fighter.actor.physics_enabled = self.original_physics

    def cycle(self, direction=1):
        choices = [f.key for f in self.session.fighters.values()
                   if f is not self.local and f.alive and not f.spectator]
        if not choices:
            return self.choose(True)
        target = self.local.spectator_target
        index = choices.index(target) if target in choices else (-1 if direction > 0 else 0)
        return self.choose(True, choices[(index+direction) % len(choices)])

    def follow(self, fighter):
        import glm
        target = self.session.fighters.get(fighter.spectator_target)
        if target is None or not target.alive or target.spectator or target is fighter:
            fighter.spectator_target = None
            return False
        fighter.actor.pos = target.actor.pos + glm.vec3(0, target.actor.view_height-fighter.actor.view_height, 0)
        fighter.actor.angle, fighter.actor.pitch = target.actor.angle, target.actor.pitch
        fighter.actor.velocity = glm.vec3(0)
        return True

    @staticmethod
    def fly(actor, delta, keys):
        import glm
        forward = glm.vec3(math.sin(actor.angle)*math.cos(actor.pitch), math.sin(actor.pitch),
                           math.cos(actor.angle)*math.cos(actor.pitch))
        right = glm.vec3(math.cos(actor.angle), 0, -math.sin(actor.angle))
        move = forward*(int(0x57 in keys)-int(0x53 in keys))
        move += right*(int(0x41 in keys)-int(0x44 in keys))
        move.y += int(0x20 in keys)-int(0x43 in keys)
        length = glm.length(move)
        if length > 1:
            move /= length
        actor.pos += move*min(max(delta, 0), .1)*(600 if 0x01000020 in keys else 300)
        actor.velocity = glm.vec3(0)
        actor.on_ground, actor.ground_object = False, None

    def primary_input(self, original, delta, keys, mouse_dx, mouse_dy):
        fighter = self.local
        if not fighter.spectator:
            return original(delta, keys, mouse_dx, mouse_dy)
        if not self.follow(fighter):
            fighter.actor.angle -= mouse_dx*.002
            fighter.actor.pitch = max(-1.5, min(1.5, fighter.actor.pitch-mouse_dy*.002))
            self.fly(fighter.actor, delta, keys)

    def render(self, original):
        result = original()
        state = self.session.logic.game_state.get_write_state()
        state.local_spectator = self.local.spectator
        from engine.glasses import normalize_glasses
        self.local.glasses_style = normalize_glasses(self.session.logic.player_runtime.glasses_style)
        state.spectator_glasses = tuple((tuple(float(v) for v in self.session.eye(f)), f.glasses_style)
            for f in self.session.fighters.values() if f.spectator and f is not self.local)
        if self.local.spectator:
            state.active_weapon, state.shot_ready, state.hud_health_alpha = None, False, 0
        return result

    def stop(self):
        if self.local.spectator:
            self.local.actor.physics_enabled = self.original_physics
            self.session.logic.camera.camera_mode = self.original_camera
