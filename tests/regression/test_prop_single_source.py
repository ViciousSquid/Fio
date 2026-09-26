"""Prop is one class in every execution context (editor, editor-play, player)."""
import os
import subprocess
import sys

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _run(code):
    result = subprocess.run([sys.executable, "-c", code], cwd=ROOT,
                            capture_output=True, text=True, timeout=120,
                            env={**os.environ, "QT_QPA_PLATFORM": "offscreen"})
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout.strip().splitlines()[-1]


@pytest.mark.qt
def test_every_import_path_yields_the_same_class():
    import editor.things
    import plugins.entitybase
    from engine.prop_entity import Prop, EDITOR_TIER

    assert editor.things.Prop is Prop
    assert plugins.entitybase.Prop is Prop
    assert EDITOR_TIER and issubclass(Prop, editor.things.Model)


@pytest.mark.qt
def test_map_load_resolves_prop_before_anything_imports_it():
    out = _run(
        "from editor.things import Thing\n"
        "t = Thing.from_dict({'type': 'prop', 'pos': [0, 0, 0],"
        " 'properties': {'type': 'prop', 'mass': 3.0}})\n"
        "import engine.prop_entity as pe\n"
        "print(type(t) is pe.Prop, t.properties['mass'], t.properties['friction'])"
    )
    assert out == "True 3.0 0.55"


def test_headless_player_gets_the_same_contract_without_pyqt():
    out = _run(
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('PyQt5', 'OpenGL'):\n"
        "            raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "from engine.prop_entity import Prop, PROP_DEFAULTS, EDITOR_TIER\n"
        "import plugins.entitybase as eb\n"
        "p = Prop(pos=[0, 0, 0])\n"
        # render_mode is derived from the authored assets rather than taken
        # literally from PROP_DEFAULTS, so it is checked separately below --
        # the point of this test is that the headless tier derives it exactly
        # as the editor tier does.
        "fixed = {k: v for k, v in PROP_DEFAULTS.items() if k != 'render_mode'}\n"
        "ok = all(p.properties[k] == v for k, v in fixed.items())\n"
        "print(EDITOR_TIER, eb.Prop is Prop, issubclass(Prop, eb.Model), ok,"
        " p.properties['render_mode'], 'PyQt5' in sys.modules)"
    )
    assert out == "False True True True billboard False"


def test_the_implied_render_mode_is_the_same_on_both_tiers():
    """A derived default is still part of the one shared contract."""
    editor_tier = _run(
        "from engine.prop_entity import Prop\n"
        "print(Prop(pos=[0, 0, 0]).properties['render_mode'],"
        " Prop(pos=[0, 0, 0], properties={'model_path': 'm.obj'})"
        ".properties['render_mode'])"
    )
    headless = _run(
        "import sys\n"
        "class Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in ('PyQt5', 'OpenGL'):\n"
        "            raise ModuleNotFoundError(name)\n"
        "sys.meta_path.insert(0, Block())\n"
        "from engine.prop_entity import Prop\n"
        "print(Prop(pos=[0, 0, 0]).properties['render_mode'],"
        " Prop(pos=[0, 0, 0], properties={'model_path': 'm.obj'})"
        ".properties['render_mode'])"
    )
    assert editor_tier == headless == "billboard model"
