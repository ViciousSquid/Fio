"""Item definitions: what a pickup gives and what the player holds.

Fio has four stable item slots. Their ids never change, so maps refer to them
(``Prop.collect_item``) and the player's loadout carries them; what each one
*is* comes from its definition:

========  ===========  ======================================================
slot      id           default definition
========  ===========  ======================================================
1         ``gun1``     Pistol -- hitscan, infinite ammo
2         ``gun2``     Shotgun -- hitscan, 1 s cooldown, uses ammo
3         ``custom1``  Cigarette -- held and shown, never fires
4         ``custom2``  Wine Glass -- held and shown, never fires
========  ===========  ======================================================

``gun1`` and ``gun2`` are Fio's built-in weapons. ``custom1`` and ``custom2``
are user-configurable: a map can redefine either one -- its name, its world
and HUD sprites, and whether it is a **weapon** (with a declarative weapon
behaviour) or a **pickup** (with a declarative effect). The definitions of the
custom slots are saved with the map (top-level ``items``).

Two forms:

* the **authoring** form is a plain dict (:data:`DEFAULT_DEFINITIONS`), held by
  :class:`ItemDefinitions` on the editor state and saved with the map;
* the **runtime** form is :class:`Item` with a :class:`WeaponSpec` or a
  :class:`PickupSpec`: frozen, validated and typed once by
  :func:`compile_definition`, looked up by id through :class:`ItemRegistry`.
  Nothing reads the authoring dicts during play.

A definition that does not validate does not compile: its id resolves to
``None`` and its error is kept on the registry. An invalid custom item is
simply not an item -- it never stands in as another one.

GL-free and Qt-free.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass

#: The four stable item ids, in slot order (number keys 1-4).
ITEM_IDS = ('gun1', 'gun2', 'custom1', 'custom2')
#: The slots a map may redefine.
CUSTOM_ITEM_IDS = ('custom1', 'custom2')

ITEM_KINDS = ('weapon', 'pickup')
WEAPON_MODES = ('none', 'hitscan', 'projectile', 'melee')
PICKUP_ACTIVATIONS = ('walk_over', 'use')
PICKUP_EFFECTS = ('health', 'ammo', 'armor', 'weapon', 'key')
HUD_ALIGNS = ('right', 'center', 'left')

_WEAPON_DEFAULTS = {
    'mode': 'none',
    'damage': 0,
    'range': 10000.0,
    'cooldown': 0.0,
    'pellets': 1,
    'spread': 0.0,
    'ammo': 0,
    'ammo_per_shot': 0,
    'sound': '',
    'noise': 0.0,
    'muzzle_flash': '',
    'projectile_speed': 900.0,
}

_PICKUP_DEFAULTS = {
    'activation': 'walk_over',
    'effect': 'health',
    'amount': 25,
    'item_id': '',
    'key_name': 'blue_key',
    'respawns': False,
    'respawn_time': 20.0,
}


def _definition(name, world_sprite, hud_sprite, hud_align='right',
                hud_height=200.0, **weapon):
    return {
        'name': name,
        'kind': 'weapon',
        'world_sprite': world_sprite,
        'hud_sprite': hud_sprite,
        'hud_align': hud_align,
        'hud_height': hud_height,
        'weapon': {**_WEAPON_DEFAULTS, **weapon},
        'pickup': dict(_PICKUP_DEFAULTS),
    }


#: The authoring form of every slot as Fio ships it. The values of gun1 and
#: gun2 are the pistol's and the shotgun's gameplay exactly.
DEFAULT_DEFINITIONS = {
    'gun1': _definition(
        'Pistol', 'assets/sprites/gun1.png', 'assets/sprites/gun1HUD.png',
        mode='hitscan', damage=25, sound='shoot.wav', noise=1.0,
        muzzle_flash='assets/sprites/gun1HUD_flash.png'),
    'gun2': _definition(
        'Shotgun', 'assets/sprites/gun2.png', 'assets/sprites/gun2HUD.png',
        hud_align='center',
        mode='hitscan', damage=75, cooldown=1.0, ammo=8, ammo_per_shot=1,
        sound='shoot2.wav', noise=1.0,
        muzzle_flash='assets/sprites/gun2HUD_flash.png'),
    'custom1': _definition(
        'Cigarette', 'assets/sprites/custom1.png',
        'assets/sprites/custom1HUD.png'),
    'custom2': _definition(
        'Wine Glass', 'assets/sprites/custom2.png',
        'assets/sprites/custom2HUD.png', hud_align='left', hud_height=220.0),
}


def default_definition(item_id):
    """A fresh copy of the shipped definition of slot *item_id*."""
    return copy.deepcopy(DEFAULT_DEFINITIONS[item_id])


# ---------------------------------------------------------------------------
# Runtime form
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class WeaponSpec:
    """How a held item fires. ``mode == 'none'`` holds it without firing."""
    mode: str
    #: Whole hit points, as monster health is.
    damage: int
    range: float
    cooldown: float
    pellets: int
    #: Cone half-angle in degrees each pellet is scattered within.
    spread: float
    #: Ammunition (the player's one pool) granted the first time the player
    #: picks the weapon up.
    ammo: int
    #: Ammunition each shot spends; 0 never runs out.
    ammo_per_shot: int
    sound: str
    #: Loudness monsters hear a shot at (0 is silent).
    noise: float
    #: HUD sprite drawn over the item while it fires; '' shows none.
    muzzle_flash: str
    projectile_speed: float

    @property
    def fires(self):
        return self.mode != 'none'


@dataclass(frozen=True, slots=True)
class PickupSpec:
    """What collecting an item does, and how it is collected."""
    activation: str
    effect: str
    amount: int
    #: The weapon an ``effect == 'weapon'`` pickup gives.
    item_id: str
    key_name: str
    respawns: bool
    respawn_time: float


@dataclass(frozen=True, slots=True)
class Item:
    id: str
    name: str
    kind: str
    world_sprite: str
    hud_sprite: str
    hud_align: str
    #: HUD sprite height on a 600-pixel-tall view (scaled with the view).
    hud_height: float
    weapon: WeaponSpec | None
    pickup: PickupSpec | None


class ItemDefinitionError(ValueError):
    pass


def _text(value, field):
    if not isinstance(value, str):
        raise ItemDefinitionError(f"{field} must be text, not {type(value).__name__}")
    return value.strip()


def _choice(value, field, choices):
    value = _text(value, field)
    if value not in choices:
        raise ItemDefinitionError(f"{field} must be one of {', '.join(choices)}, not {value!r}")
    return value


def _number(value, field, minimum=0.0, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ItemDefinitionError(f"{field} must be a number, not {value!r}")
    if value != value or value in (float('inf'), float('-inf')):
        raise ItemDefinitionError(f"{field} must be finite")
    if value < minimum:
        raise ItemDefinitionError(f"{field} must be at least {minimum:g}, not {value!r}")
    if integer:
        if float(value) != int(value):
            raise ItemDefinitionError(f"{field} must be a whole number, not {value!r}")
        return int(value)
    return float(value)


def _flag(value, field):
    if not isinstance(value, bool):
        raise ItemDefinitionError(f"{field} must be true or false, not {value!r}")
    return value


def _section(definition, key, defaults):
    section = definition.get(key, {})
    if not isinstance(section, dict):
        raise ItemDefinitionError(f"{key} must be an object")
    unknown = set(section) - set(defaults)
    if unknown:
        raise ItemDefinitionError(f"{key} has unknown settings: {', '.join(sorted(unknown))}")
    return {**defaults, **section}


def compile_definition(item_id, definition):
    """The runtime :class:`Item` for one authoring definition.

    Raises :class:`ItemDefinitionError` naming the first thing wrong. Missing
    settings take the defaults of an unarmed, display-only item -- never
    another slot's values.
    """
    if item_id not in ITEM_IDS:
        raise ItemDefinitionError(f"unknown item id {item_id!r}")
    if not isinstance(definition, dict):
        raise ItemDefinitionError("a definition must be an object")
    known = {'name', 'kind', 'world_sprite', 'hud_sprite', 'hud_align',
             'hud_height', 'weapon', 'pickup'}
    unknown = set(definition) - known
    if unknown:
        raise ItemDefinitionError(f"unknown settings: {', '.join(sorted(unknown))}")
    name = _text(definition.get('name', ''), 'name')
    if not name:
        raise ItemDefinitionError("name must not be empty")
    kind = _choice(definition.get('kind', 'weapon'), 'kind', ITEM_KINDS)
    weapon = pickup = None
    if kind == 'weapon':
        w = _section(definition, 'weapon', _WEAPON_DEFAULTS)
        weapon = WeaponSpec(
            mode=_choice(w['mode'], 'weapon.mode', WEAPON_MODES),
            damage=_number(w['damage'], 'weapon.damage', integer=True),
            range=_number(w['range'], 'weapon.range', minimum=1.0),
            cooldown=_number(w['cooldown'], 'weapon.cooldown'),
            pellets=_number(w['pellets'], 'weapon.pellets', minimum=1, integer=True),
            spread=_number(w['spread'], 'weapon.spread'),
            ammo=_number(w['ammo'], 'weapon.ammo', integer=True),
            ammo_per_shot=_number(w['ammo_per_shot'], 'weapon.ammo_per_shot', integer=True),
            sound=_text(w['sound'], 'weapon.sound'),
            noise=_number(w['noise'], 'weapon.noise'),
            muzzle_flash=_text(w['muzzle_flash'], 'weapon.muzzle_flash'),
            projectile_speed=_number(w['projectile_speed'], 'weapon.projectile_speed',
                                     minimum=1.0),
        )
    else:
        p = _section(definition, 'pickup', _PICKUP_DEFAULTS)
        effect = _choice(p['effect'], 'pickup.effect', PICKUP_EFFECTS)
        item = _text(p['item_id'], 'pickup.item_id')
        if effect == 'weapon' and item not in ITEM_IDS:
            raise ItemDefinitionError(
                f"pickup.item_id must name an item ({', '.join(ITEM_IDS)}), not {item!r}")
        key_name = _text(p['key_name'], 'pickup.key_name')
        if effect == 'key' and not key_name:
            raise ItemDefinitionError("pickup.key_name must not be empty")
        pickup = PickupSpec(
            activation=_choice(p['activation'], 'pickup.activation', PICKUP_ACTIVATIONS),
            effect=effect,
            amount=_number(p['amount'], 'pickup.amount', integer=True),
            item_id=item,
            key_name=key_name,
            respawns=_flag(p['respawns'], 'pickup.respawns'),
            respawn_time=_number(p['respawn_time'], 'pickup.respawn_time'),
        )
    return Item(
        id=item_id, name=name, kind=kind,
        world_sprite=_text(definition.get('world_sprite', ''), 'world_sprite'),
        hud_sprite=_text(definition.get('hud_sprite', ''), 'hud_sprite'),
        hud_align=_choice(definition.get('hud_align', 'right'), 'hud_align', HUD_ALIGNS),
        hud_height=_number(definition.get('hud_height', 200.0), 'hud_height', minimum=1.0),
        weapon=weapon, pickup=pickup)


def complete_definition(item):
    """The full authoring definition of compiled *item*.

    Every setting is present; the section its kind does not use is at its
    defaults. What an editor shows for a definition that may leave settings out.
    """
    weapon, pickup = item.weapon, item.pickup
    return {
        'name': item.name,
        'kind': item.kind,
        'world_sprite': item.world_sprite,
        'hud_sprite': item.hud_sprite,
        'hud_align': item.hud_align,
        'hud_height': item.hud_height,
        'weapon': ({key: getattr(weapon, key) for key in _WEAPON_DEFAULTS}
                   if weapon is not None else dict(_WEAPON_DEFAULTS)),
        'pickup': ({key: getattr(pickup, key) for key in _PICKUP_DEFAULTS}
                   if pickup is not None else dict(_PICKUP_DEFAULTS)),
    }


class ItemRegistry:
    """The compiled items of one session, by id."""

    __slots__ = ('_items', 'errors')

    def __init__(self, items, errors=None):
        self._items = dict(items)
        #: ``id -> message`` for each definition that did not compile.
        self.errors = dict(errors or {})

    @classmethod
    def compile(cls, definitions):
        items, errors = {}, {}
        for item_id in ITEM_IDS:
            try:
                items[item_id] = compile_definition(item_id, definitions[item_id])
            except ItemDefinitionError as exc:
                errors[item_id] = str(exc)
        return cls(items, errors)

    def resolve(self, item_id):
        """The :class:`Item` for *item_id*, or None (unknown, or invalid)."""
        return self._items.get(item_id)

    def __iter__(self):
        return (self._items[i] for i in ITEM_IDS if i in self._items)


#: The registry of the shipped definitions, for a world that defines none.
DEFAULT_REGISTRY = ItemRegistry.compile(DEFAULT_DEFINITIONS)


# ---------------------------------------------------------------------------
# Authoring form
# ---------------------------------------------------------------------------

class ItemDefinitions:
    """The item definitions of the open world, as authored.

    Built-in slots keep their shipped definitions; the custom slots hold
    whatever the author set, saved with the map as ``items``. ``version``
    changes on every change, so views and caches can tell.
    """

    def __init__(self):
        self._custom = {item_id: default_definition(item_id) for item_id in CUSTOM_ITEM_IDS}
        self.version = 0
        self._registry = (None, None)

    def definition(self, item_id):
        """A copy of the authoring definition of *item_id*."""
        if item_id in self._custom:
            return copy.deepcopy(self._custom[item_id])
        return default_definition(item_id)

    def name(self, item_id):
        """The user-facing name of *item_id* (the id itself if unknown)."""
        if item_id in self._custom:
            return str(self._custom[item_id].get('name') or item_id)
        if item_id in DEFAULT_DEFINITIONS:
            return DEFAULT_DEFINITIONS[item_id]['name']
        return str(item_id)

    def set_custom(self, item_id, definition):
        """Replace a custom slot's definition. Raises if it does not compile."""
        if item_id not in CUSTOM_ITEM_IDS:
            raise ItemDefinitionError(f"{item_id!r} is not a custom item slot")
        compile_definition(item_id, definition)
        self._custom[item_id] = copy.deepcopy(definition)
        self.version += 1

    def reset(self):
        self._custom = {item_id: default_definition(item_id) for item_id in CUSTOM_ITEM_IDS}
        self.version += 1

    def load(self, items):
        """Take the custom definitions saved with a map (``None``: defaults).

        A definition is kept as authored even if it does not compile; the
        registry reports it and resolves it to nothing.
        """
        self._custom = {item_id: default_definition(item_id) for item_id in CUSTOM_ITEM_IDS}
        for item_id, definition in (items or {}).items():
            if item_id in CUSTOM_ITEM_IDS and isinstance(definition, dict):
                self._custom[item_id] = copy.deepcopy(definition)
        self.version += 1

    def to_level_data(self):
        """The custom definitions that differ from the shipped ones."""
        return {item_id: copy.deepcopy(definition)
                for item_id, definition in self._custom.items()
                if definition != DEFAULT_DEFINITIONS[item_id]}

    def registry(self):
        """The compiled :class:`ItemRegistry`, rebuilt only after a change."""
        version, registry = self._registry
        if version != self.version:
            definitions = {item_id: self.definition(item_id) for item_id in ITEM_IDS}
            registry = ItemRegistry.compile(definitions)
            self._registry = (self.version, registry)
        return registry
