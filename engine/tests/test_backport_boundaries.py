"""Guard tests for the MiniWind -> Fio back-port.

Two jobs:
  1. Fio must remain a generic engine/editor: no RPG concepts, no dependency on
     a `game` package, no MiniWind branding.
  2. Fio-only functionality that MiniWind lacks (or has an older version of)
     must still be present after the back-port.
"""

import os
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[2]

# Generic modules that must never reach into a game layer or speak RPG.
GENERIC_MODULES = [
    "engine/render_cull.py",
    "engine/floating_windows.py",
    "engine/constants.py",
    "engine/physics.py",
    "engine/player.py",
    "engine/renderer_F.py",
    "engine/terrain.py",
    "engine/glb_loader.py",
    "editor/editor_state.py",
    "editor/asset_browser.py",
    "editor/things.py",
    "plugins/manager.py",
    "plugins/api.py",
    "plugins/integration.py",
]

RPG_TERMS = [
    "quest", "dialogue", "faction", "disposition", "birthsign", "bestiary",
    "spell", "companion", "inventory", "gib", "blood stain", "hit_flash",
    "stuck_arrow", "diceroll", "dice_roll", "request_dice_roll",
    "miniwind", "npc",
]

# MiniWind's branding reds. Fio's accent is #F08000.
BRANDING_COLOURS = ["#d61604", "#b52316", "#a10a28", "#C41E3A", "#c41e3a"]


def _read(rel):
    return (ROOT / rel).read_text(encoding="utf-8", errors="replace")


def _iter_source():
    for p in ROOT.rglob("*.py"):
        if ".git" in p.parts:
            continue
        yield p


# ---------------------------------------------------------------------------
# RPG boundary
# ---------------------------------------------------------------------------

def test_generic_modules_carry_no_rpg_vocabulary():
    # Word-boundary match: "quest" must not fire on "request", "npc" not on
    # arbitrary substrings, and so on.
    patterns = [(t, re.compile(r"\b" + re.escape(t) + r"\b", re.I)) for t in RPG_TERMS]
    # Pre-existing Fio prose that merely *mentions* an RPG concept as an
    # example is not RPG machinery and predates this back-port.
    ALLOWED = {
        ("editor/things.py", "quest"),   # LogicKeyValueStore docstring example
    }
    offenders = []
    for rel in GENERIC_MODULES:
        src = _read(rel)
        for term, pat in patterns:
            if pat.search(src) and (rel, term) not in ALLOWED:
                offenders.append((rel, term))
    assert not offenders, f"RPG vocabulary leaked into generic modules: {offenders}"


def test_allowlisted_rpg_mentions_are_prose_only():
    """The one allowed 'quest' mention must stay a comment, never code."""
    for line in _read("editor/things.py").splitlines():
        if re.search(r"\bquest\b", line, re.I):
            stripped = line.strip()
            assert not stripped.startswith(("def ", "class ", "import ", "from ")), \
                f"quest appears in code, not prose: {line!r}"


def test_no_module_imports_a_game_package():
    """Nothing in Fio may import MiniWind's `game` package."""
    pattern = re.compile(r"^\s*(from\s+game[\s.]|import\s+game\b)", re.M)
    offenders = [str(p.relative_to(ROOT))
                 for p in _iter_source() if pattern.search(
                     p.read_text(encoding="utf-8", errors="replace"))]
    assert not offenders, f"game/ imports found: {offenders}"


def test_no_miniwind_branding_colours():
    offenders = []
    for p in _iter_source():
        if p.resolve() == pathlib.Path(__file__).resolve():
            continue          # this guard file necessarily names the colours
        src = p.read_text(encoding="utf-8", errors="replace")
        for colour in BRANDING_COLOURS:
            if colour in src:
                offenders.append((str(p.relative_to(ROOT)), colour))
    assert not offenders, f"MiniWind branding colours found: {offenders}"


def test_fio_accent_colour_survived():
    """The back-port must not have recoloured Fio's orange accent."""
    assert "#F08000" in _read("editor/ui.py")
    assert "#F08000" in _read("editor/property_editor.py")


def test_window_title_is_still_fio():
    src = _read("editor/main_window.py")
    assert 'self.setWindowTitle("Fio")' in src
    assert "MiniWind" not in src


def test_no_rpg_io_definitions_were_registered():
    """RollDice / OnDiceRolled must not exist in the generic I/O registry."""
    src = _read("editor/io_system.py")
    for banned in ("RollDice", "OnDiceRolled", "OnDiceSuccess", "OnDiceFailure"):
        assert banned not in src, f"{banned} leaked into io_system"


def test_settings_window_has_no_game_tab():
    src = _read("editor/SettingsWindow.py")
    assert "_create_game_tab" not in src
    assert "visualise_dice_rolls" not in src


def test_sprite_shader_has_no_rpg_uniforms():
    """sprite_rot / sprite_tint exist only to serve head sprites + hit flashes."""
    src = _read("engine/shaders.py")
    assert "sprite_rot" not in src
    assert "sprite_tint" not in src


def test_threaded_game_state_has_no_combat_intents():
    src = _read("engine/threaded_game_state.py")
    for banned in ("queue_rpg_attack", "consume_rpg_attack", "queue_rpg_cast",
                   "stuck_arrows", "blood_stains"):
        assert banned not in src, f"{banned} leaked into threaded_game_state"


def test_console_has_no_rpg_commands():
    src = _read("editor/console_commands.py")
    for banned in ("cmd_quest", "cmd_diceroll", "cmd_inspect"):
        assert banned not in src, f"{banned} leaked into console_commands"


def test_manager_has_no_rpg_inspector_provider():
    src = _read("plugins/manager.py")
    assert "register_inspector_provider" not in src
    assert "inspector_snapshot" not in src


# ---------------------------------------------------------------------------
# Fio-only functionality preserved
# ---------------------------------------------------------------------------

def test_angled_brush_geometry_helpers_survive():
    src = _read("engine/brush_geometry.py")
    for fn in ("def face_key(", "def iter_surface_faces(", "def find_surface_face(",
               "def face_plane_index(", "def ray_convex_face(", "def _ray_triangle("):
        assert fn in src, f"Fio-only geometry helper missing: {fn}"


def test_cut_face_highlight_and_texturing_survive():
    core = _read("engine/renderer_core.py")
    assert "_geo_face_highlight_verts" in core
    assert "_draw_face_highlight_verts" in core
    assert "_geo_run_plane" in core

    # Addressing a cut face's texture moved out of the Surface Inspector into
    # a shared, Qt-free layer, so the inspector and anything else reach a cut
    # face the same way.  The capability is what matters, not where it lives.
    face_tex = _read("editor/face_texture.py")
    assert "def face_plane(" in face_tex
    assert "def is_cut_face(" in face_tex
    inspector = _read("editor/surface_inspector.py")
    assert "face_texture" in inspector

    mw = _read("editor/main_window.py")
    assert "brush_geometry.face_plane_index(brush, face_name)" in mw


def test_procedural_map_generation_survives():
    mw = _read("editor/main_window.py")
    assert "def show_procedural_map_generator(" in mw
    assert "def load_procedural_map(" in mw
    ui = _read("editor/ui.py")
    assert "procedural_action" in ui
    assert (ROOT / "editor" / "procedural_generator.py").exists()


def test_io_widget_rewrite_survives():
    """The recent compact-table IO widget must not be reverted to the older one."""
    src = _read("editor/io_editor_widget.py")
    assert "self._row_height = self.table.fontMetrics().height() + 12" in src
    assert "setSectionResizeMode(QHeaderView.Fixed)" in src


def test_renderer_light_capacities_untouched():
    src = _read("engine/renderer_core.py")
    assert "MAX_LIGHTS = 32" in src
    assert "MAX_SHADOW_LIGHTS = 8" in src


def test_thing_counter_regression_not_imported():
    """MiniWind nests the counter loop inside the entity loop; Fio must not."""
    src = _read("editor/things.py")
    assert "cls._counters[class_name] = max_indices[class_name]" in src
    assert "Thing._counters[class_name] = max_indices.get(class_name, 0) + 1" not in src


def test_fire_output_was_not_replaced_by_dice():
    """MiniWind's api.py drops fire_output in favour of request_dice_roll."""
    src = _read("plugins/api.py")
    assert "def fire_output(" in src
    assert "request_dice_roll" not in src


def test_package_exporter_still_collects_custom_dead():
    """MiniWind removed this key; Fio's Monster still uses it."""
    src = _read("editor/package_exporter.py")
    assert "custom_dead" in src


def test_camera_mode_default_is_first_person():
    src = _read("editor/ui.py")
    assert 'MainWindow.camera_mode_combobox.setCurrentText("First Person")' in src
