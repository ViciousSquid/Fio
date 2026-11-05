# SPDX-License-Identifier: GPL-2.0-or-later
# Flag rules adapted from id Software Quake III Arena code/game/g_team.c.
# Original Copyright (C) 1999-2005 id Software, Inc.; see NOTICE.md and LICENSE.
"""Red/blue teams and Quake III-style two-flag Capture the Flag."""
from dataclasses import dataclass
import copy

from engine.change_journal import touch

from .entities import DeathmatchFlag
from .navigation import distance


TEAMS = ("red", "blue")


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
        if flag.carrier in self.session.fighters:
            self.session.fighters[flag.carrier].carrying = None
        flag.pos, flag.carrier, flag.dropped, flag.return_at = flag.home, None, False, 0
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
        if not fighter.carrying:
            return
        flag = self.flags[fighter.carrying]
        fighter.carrying = None
        flag.carrier, flag.dropped = None, True
        pos = self.session.feet(fighter)
        if pos[1] < -1024:
            self.return_flag(flag)
            return
        ground = self.session.logic.session_runtime.spatial_grid.raycast_down(pos[0], pos[2], pos[1] + 10)
        if ground is None:
            self.return_flag(flag)
            return
        flag.pos, flag.return_at = (pos[0], ground, pos[2]), self.session.clock + self.return_delay
        self.draw(flag)
        self.session.output(flag.entity, "OnDrop")

    def touch(self, fighter, flag):
        """Port of Team_TouchOurFlag / Team_TouchEnemyFlag rule ordering."""
        s = self.session
        if not fighter.alive or flag.carrier is not None:
            return
        if fighter.team == flag.team:
            if flag.dropped:
                fighter.objective_score += 1  # CTF_RECOVERY_BONUS
                self.return_flag(flag)
                return
            if fighter.carrying:
                stolen = self.flags[fighter.carrying]
                fighter.captures += 1
                fighter.objective_score += 5  # CTF_CAPTURE_BONUS
                self.scores[fighter.team] += 1
                self.return_flag(stolen, announce=False)
                s.feed.append((s.clock + 5, f"{fighter.name} captured for {fighter.team}!"))
                s.output(flag.entity, "OnCapture", fighter.name)
                s.emit("deathmatch.capture", team=fighter.team, fighter=fighter.key)
                if self.capture_limit and self.scores[fighter.team] >= self.capture_limit:
                    s.finish("Capture limit")
            return
        if fighter.carrying is None and fighter.team in TEAMS:
            # Taking the flag removes spawn protection, as firing does.
            fighter.carrying, flag.carrier = flag.team, fighter.key
            fighter.protected_until = 0
            flag.dropped, flag.return_at = False, 0
            self.draw(flag)
            s.feed.append((s.clock + 5, f"{fighter.name} took the {flag.team} flag"))
            s.output(flag.entity, "OnTaken", fighter.name)

    def step(self):
        if not self.ctf:
            return
        s = self.session
        for flag in self.flags.values():
            if flag.carrier is not None:
                carrier = s.fighters.get(flag.carrier)
                if carrier is None or not carrier.alive:
                    if carrier is not None:
                        self.drop(carrier)
                    else:
                        self.return_flag(flag)
                else:
                    flag.pos = s.feet(carrier)
            elif flag.dropped and s.clock >= flag.return_at:
                self.return_flag(flag)
            self.draw(flag)
        # Own dropped-flag returns precede captures for deterministic touches.
        for fighter in list(s.fighters.values()):
            if not fighter.alive or fighter.team not in TEAMS:
                continue
            for flag in sorted(self.flags.values(), key=lambda f: f.team != fighter.team):
                if flag.carrier is None and distance(s.feet(fighter), flag.pos) <= 60:
                    a, b = s.eye(fighter), (flag.pos[0], flag.pos[1] + 40, flag.pos[2])
                    if s.visible_points(a, b):
                        self.touch(fighter, flag)
                        if s.finished:
                            return

    def objective(self, fighter):
        """Carriers return; defenders recover flags; attackers raid/escort."""
        if not self.ctf or fighter.team not in self.flags:
            return None
        own = self.flags[fighter.team]
        enemy = self.flags["blue" if fighter.team == "red" else "red"]
        if fighter.carrying:
            return own.pos if own.dropped else own.home
        if own.dropped:
            return own.pos
        if own.carrier:
            carrier = self.session.fighters.get(own.carrier)
            return self.session.feet(carrier) if carrier else own.home
        if enemy.carrier:
            carrier = self.session.fighters.get(enemy.carrier)
            if carrier and carrier is not fighter:
                p = self.session.feet(carrier)
                return (p[0] + 90, p[1], p[2] + 90)
        # Every third bot defends; the rest attack. Remote/local humans use input.
        bots = [f for f in self.session.fighters.values() if f.entity is not None and not f.spectator and f.team == fighter.team]
        if len(bots) > 2 and bots.index(fighter) % 3 == 2:
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
