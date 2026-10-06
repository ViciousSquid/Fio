"""Every map shipped in ``maps/`` points at files and ids that exist.

A rename that misses a shipped map is invisible until someone plays it: the
showcase kept a pickup for the removed ``cig`` weapon (a missing sprite, and a
weapon the engine no longer knew was non-firing) and a Speaker reading
``assets/sound/`` instead of ``assets/sounds/``.
"""

import glob
import json
import os

import pytest

from engine.prop_entity import GUN_NAMES

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MAPS = sorted(glob.glob(os.path.join(ROOT, "maps", "*.json")))
REFERENCE_ROOTS = ("assets/", "cutscenes/")


def _strings(node):
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def _things(data):
    things = data.get("things") or []
    return [t for t in things if isinstance(t, dict)]


def _properties(thing):
    props = thing.get("properties")
    return props if isinstance(props, dict) else thing


def test_maps_are_shipped():
    assert MAPS, "no maps found - this test is looking in the wrong place"


@pytest.mark.parametrize("path", MAPS, ids=os.path.basename)
def test_referenced_files_exist(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    # Authored paths may use either separator; the loaders normalise both.
    references = {value.replace("\\", "/") for value in _strings(data)}
    missing = sorted(
        value for value in references
        if value.startswith(REFERENCE_ROOTS)
        and os.path.splitext(value)[1]
        and not os.path.isfile(os.path.join(ROOT, *value.split("/")))
    )
    assert not missing, f"{os.path.basename(path)} references missing files: {missing}"


@pytest.mark.parametrize("path", MAPS, ids=os.path.basename)
def test_weapon_pickups_name_known_weapons(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    unknown = sorted({
        str(_properties(t).get("collect_weapon"))
        for t in _things(data)
        if _properties(t).get("collect_type") == "weapon"
        and _properties(t).get("collect_weapon", "gun1") not in GUN_NAMES
    })
    assert not unknown, f"{os.path.basename(path)} has pickups for unknown weapons: {unknown}"
