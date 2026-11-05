# SPDX-License-Identifier: GPL-2.0-or-later
"""Red/blue teams and a contact-driven Capture the Flag state machine."""
from dataclasses import dataclass
import copy

from engine.change_journal import touch

from .entities import DeathmatchFlag
from .navigation import distance


TEAMS = ("red", "blue")
RECOVERY_POINTS = 1
CAPTURE_POINTS = 5

# (touching own flag, current flag state) -> requested transition.
CONTACT_ACTIONS = {
    (True, "home"): "capture",
    (True, "dropped"): "recover",
    (False, "home"): "take",
    (False, "dropped"): "take",
}


@dataclass
class Flag:
    team: str
    entity: object
    home: tuple
    pos: tuple
    carrier: str | None = None
    dropped: bool = False
    return_at: float = 0.0
    light: object = None

    @property
    def state(self):
        if self.carrier is not None:
            return "carried"
        return "dropped" if self.dropped else "home"


class TeamGame:
    def __init__(self, session):
        self.session = session
        p = session.settings.properties
        self.mode = p.get("game_type", "deathmatch")
        self.enabled = self.mode in ("teamdeathmatch", "ctf")
        self.ctf = self.mode == "ctf"
        self.friendly_fire = bool(p.get("friendly_fire", False))
        self.capture_limit = max(0, int(p.get("capture_limit", 10)))
        self.return_delay = max(1, min(300, float(p.get("flag_return_time", 40))))
        self.flag_shadows = False
        self.scores = dict.fromkeys(TEAMS, 0)
        self.flags = {}
        self._authored = []
        for entity in session.logic.editor_state.things:
            if not isinstance(entity, DeathmatchFlag):
                continue
            team = entity.properties.get("team")
            if team not in TEAMS or team in self.flags:
                raise ValueError("A CTF map requires exactly one red and one blue flag.")
            home = tuple(entity.pos)
            self.flags[team] = Flag(team, entity, home, home)
            self._authored.append((entity, list(entity.pos), copy.deepcopy(entity.properties)))
        if self.ctf and set(self.flags) != set(TEAMS):
            raise ValueError("CTF needs one Deathmatch Flag for each team.")

    def assign(self, fighter, requested="auto"):
        if not self.enabled:
            fighter.team = ""
            if fighter.entity is not None:
                fighter.entity.properties["team"] = ""
            return
        if requested not in TEAMS:
            counts = {t: sum(f is not fighter and not f.spectator and f.team == t for f in self.session.fighters.values()) for t in TEAMS}
            requested = min(TEAMS, key=lambda t: counts[t])
        fighter.team = requested
        if fighter.entity is not None:
            fighter.entity.properties["team"] = requested
            if self.session.settings.properties.get("team_skins", True):
                fighter.entity.properties.update(monster_type="flying", variant="variant1" if requested == "blue" else "<None>")

    def allies(self, a, b):
        return self.enabled and a is not b and a.team == b.team and a.team in TEAMS

    def can_damage(self, attacker, victim):
        return attacker is None or self.friendly_fire or not self.allies(attacker, victim)

    def reset(self):
        self.scores = dict.fromkeys(TEAMS, 0)
        for f in self.session.fighters.values():
            f.carrying = None
            f.captures = f.objective_score = 0
        for flag in self.flags.values():
            self.return_flag(flag, announce=False)

    def return_flag(self, flag, announce=True):
        holder = self.session.fighters.get(flag.carrier)
        if holder is not None and holder.carrying == flag.team:
            holder.carrying = None
        flag.carrier = None
        flag.dropped = False
        flag.return_at = 0.0
        flag.pos = flag.home
        self.draw(flag)
        if announce:
            self.session.feed.append((self.session.clock + 5, f"{flag.team.title()} flag returned"))
            self.session.output(flag.entity, "OnReturn")
            self.session.emit("deathmatch.flag_return", team=flag.team)

    def draw(self, flag):
        carrier = self.session.fighters.get(flag.carrier)
        carried = carrier is not None and carrier.alive
        presentation = dict(hidden=not self.ctf, _sprite_overlay=carried,
                            sprite_size=[64, 64] if carried else [128, 128],
                            sprite_path=flag.entity.get_sprite_path())
        props = flag.entity.properties
        if any(props.get(key) != value for key, value in presentation.items()):
            props.update(presentation)
            touch(flag.entity)
        if carried:
            # Share the body's exact centre, for every heading and camera.
            # The renderer's overlay ordering keeps the icon over the body.
            flag.pos = self.session.feet(carrier)
            if carrier.entity is not None:
                flag.entity.pos = list(carrier.entity.pos)
            else:
                p = flag.pos
                flag.entity.pos = [p[0], p[1]+64, p[2]]
        else:
            p = flag.pos
            flag.entity.pos = [p[0], p[1]+64, p[2]]
        self.draw_light(flag)

    def draw_light(self, flag):
        if self.ctf and flag.light is None:
            from editor.things import Light
            colour = [255, 35, 20] if flag.team == 'red' else [40, 90, 255]
            flag.light = Light(list(flag.entity.pos), dict(
                name=f'CTF_{flag.team}_flag_light', colour=colour, state='on',
                intensity=2.0, radius=320.0, casts_shadows=False))
            self.session.logic.editor_state.things.append(flag.light)
            self.session._spawned.append(flag.light)
            self.session.logic.world_runtime.build_entity_caches()
        if flag.light is not None:
            flag.light.pos = list(flag.entity.pos)
            presentation = dict(state='on' if self.ctf else 'off', hidden=not self.ctf,
                                casts_shadows=self.flag_shadows)
            if any(flag.light.properties.get(k) != v for k, v in presentation.items()):
                flag.light.properties.update(presentation)
                touch(flag.light)

    def drop(self, fighter):
        flag = self.flags.get(fighter.carrying)
        fighter.carrying = None
        if flag is None or flag.carrier != fighter.key:
            return
        pos = self.session.feet(fighter)
        grid = self.session.logic.session_runtime.spatial_grid
        ground = None if pos[1] < -1024 else grid.raycast_down(pos[0], pos[2], pos[1] + 10)
        if ground is None:
            self.return_flag(flag)
            return
        flag.carrier = None
        flag.dropped = True
        flag.pos = (pos[0], ground, pos[2])
        flag.return_at = self.session.clock + self.return_delay
        self.draw(flag)
        self.session.output(flag.entity, "OnDrop")

    def touch(self, fighter, flag):
        """Resolve a contact using flag state and team ownership."""
        if (not self.ctf or self.session.finished or not fighter.alive
                or fighter.spectator or fighter.team not in TEAMS):
            return
        action = CONTACT_ACTIONS.get((fighter.team == flag.team, flag.state))
        if action == "recover":
            fighter.objective_score += RECOVERY_POINTS
            self.return_flag(flag)
        elif action == "take":
            self._take_flag(fighter, flag)
        elif action == "capture":
            self._capture_flag(fighter, flag)

    def _take_flag(self, fighter, flag):
        if fighter.carrying is not None:
            return
        flag.carrier = fighter.key
        flag.dropped = False
        flag.return_at = 0.0
        fighter.carrying = flag.team
        fighter.protected_until = 0
        self.draw(flag)
        session = self.session
        session.output(flag.entity, "OnTaken", fighter.name)
        session.feed.append((session.clock + 5, f"{fighter.name} took the {flag.team} flag"))

    def _capture_flag(self, fighter, base):
        cargo = self.flags.get(fighter.carrying)
        if cargo is None or cargo.team == fighter.team or cargo.carrier != fighter.key:
            return
        self.return_flag(cargo, announce=False)
        fighter.captures += 1
        fighter.objective_score += CAPTURE_POINTS
        self.scores[fighter.team] += 1
        session = self.session
        session.output(base.entity, "OnCapture", fighter.name)
        session.emit("deathmatch.capture", team=fighter.team, fighter=fighter.key)
        session.feed.append((session.clock + 5, f"{fighter.name} captured for {fighter.team}!"))
        if self.capture_limit > 0 and self.scores[fighter.team] >= self.capture_limit:
            session.finish("Capture limit")

    def step(self):
        if not self.ctf or self.session.finished:
            return
        session = self.session
        # Resolve ownership and deadlines before accepting any new contacts.
        for flag in self.flags.values():
            if flag.state == "carried":
                holder = session.fighters.get(flag.carrier)
                if holder is None or holder.carrying != flag.team:
                    self.return_flag(flag)
                elif not holder.alive or holder.spectator:
                    self.drop(holder)
            elif flag.state == "dropped" and session.clock >= flag.return_at:
                self.return_flag(flag)
            self.draw(flag)

        def contacts():
            for fighter in sorted(session.fighters.values(), key=lambda f: f.key):
                if not fighter.alive or fighter.spectator or fighter.team not in TEAMS:
                    continue
                for flag in self.flags.values():
                    if flag.state == "carried" or distance(session.feet(fighter), flag.pos) > 60:
                        continue
                    flag_eye = (flag.pos[0], flag.pos[1] + 40, flag.pos[2])
                    if session.visible_points(session.eye(fighter), flag_eye):
                        yield fighter, flag

        # All friendly recoveries happen before captures or enemy pickups.
        for fighter, flag in contacts():
            if fighter.team == flag.team and flag.state == "dropped":
                self.touch(fighter, flag)
        # Sample again because returning a flag changes its contact location.
        for fighter, flag in contacts():
            self.touch(fighter, flag)
            if session.finished:
                break

    def objective(self, fighter):
        """Select a team task from ownership, then assign idle defenders."""
        if not self.ctf or fighter.spectator or fighter.team not in self.flags:
            return None
        own = self.flags[fighter.team]
        enemy = next(flag for team, flag in self.flags.items() if team != fighter.team)
        if fighter.carrying:
            return own.pos if own.state == "dropped" else own.home
        if own.state != "home":
            thief = self.session.fighters.get(own.carrier)
            return self.session.feet(thief) if thief is not None else own.pos
        escort = self.session.fighters.get(enemy.carrier)
        if escort is not None and escort is not fighter:
            x, y, z = self.session.feet(escort)
            return (x + 90, y, z + 90)
        bots = sorted((f.key for f in self.session.fighters.values()
                       if f.role == "bot" and not f.spectator and f.team == fighter.team))
        if fighter.key in bots[2::3]:
            return own.home
        return enemy.pos

    def winner(self):
        if not self.enabled:
            return None
        if self.scores["red"] == self.scores["blue"]:
            return "Draw"
        return "Red" if self.scores["red"] > self.scores["blue"] else "Blue"

    def stop(self):
        for entity, pos, properties in self._authored:
            entity.pos = pos
            entity.properties.clear()
            entity.properties.update(properties)
            touch(entity)
