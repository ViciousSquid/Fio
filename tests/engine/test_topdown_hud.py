"""Regression tests for the top-down HUD contract.

The tests inspect _draw_hud control flow rather than constructing a QOpenGLWidget.
The HUD policy is: health, ammo, and collected keys remain visible overhead;
first-person gun artwork and the crosshair remain hidden overhead.
"""

import ast
from pathlib import Path

GAME_VIEW = Path(__file__).parents[2] / "engine" / "qt_game_view.py"


def _draw_hud_tree():
    tree = ast.parse(GAME_VIEW.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "_draw_hud":
            return node
    raise AssertionError("_draw_hud was removed or renamed")


def _parents(root):
    result = {}
    for parent in ast.walk(root):
        for child in ast.iter_child_nodes(parent):
            result[child] = parent
    return result


def _enclosing_ifs(node, parents):
    guards = []
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.If):
            guards.append(ast.unparse(node.test))
    return guards


def _nodes_with_name(root, name):
    return [node for node in ast.walk(root) if isinstance(node, ast.Name) and node.id == name]


def test_health_is_not_gated_by_topdown_weapon_suppression():
    hud = _draw_hud_tree()
    parents = _parents(hud)
    names = _nodes_with_name(hud, "health_text")
    assert names, "_draw_hud must still render the health count"
    assert all(not any("overhead" in guard for guard in _enclosing_ifs(n, parents)) for n in names)


def test_ammo_is_not_gated_by_topdown_weapon_suppression():
    hud = _draw_hud_tree()
    parents = _parents(hud)
    names = _nodes_with_name(hud, "lines")
    assert names, "_draw_hud must still render the ammo/armor lines"
    assert all(not any("overhead" in guard for guard in _enclosing_ifs(n, parents)) for n in names)


def test_collected_keys_are_not_gated_by_topdown_weapon_suppression():
    hud = _draw_hud_tree()
    parents = _parents(hud)
    names = _nodes_with_name(hud, "collected_keys")
    assert names, "_draw_hud must still process collected keys"
    assert all(not any("overhead" in guard for guard in _enclosing_ifs(n, parents)) for n in names)


def test_first_person_gun_artwork_is_explicitly_suppressed_overhead():
    hud = _draw_hud_tree()
    matches = []
    for node in ast.walk(hud):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "active_weapon" in test and "not overhead" in test and "held.hud_sprite" in ast.unparse(node):
                matches.append(node)
    assert matches, "gun HUD artwork must be inside an active_weapon and not overhead guard"


def test_first_person_crosshair_is_explicitly_suppressed_overhead():
    hud = _draw_hud_tree()
    matches = []
    for node in ast.walk(hud):
        if isinstance(node, ast.If):
            test = ast.unparse(node.test)
            if "active_weapon" in test and "not overhead" in test and "drawLine" in ast.unparse(node):
                matches.append(node)
    assert matches, "crosshair must be inside an active_weapon and not overhead guard"
