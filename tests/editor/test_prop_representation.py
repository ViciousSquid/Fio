"""Switching a Prop's representation to Model gives it something to draw.

``render_mode`` and the asset paths have to stay coherent in both directions.
A fresh Prop is a billboard because the defaults ship a sprite and no mesh;
the mirror of that is that asking for a model must produce a model, rather
than a Prop set to 'model' with an empty ``model_path`` and nothing on screen.
"""

import os
import types

import pytest

pytest.importorskip("PyQt5", reason="the property panel is editor-tier")

from editor.property_editor import PropertyEditor      # noqa: E402
from engine.prop_entity import PROP_DEFAULTS, Prop     # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def panel(qt_app):
    from PyQt5.QtWidgets import QWidget

    # update_object_prop repaints the viewports after a write, so view_3d has
    # to be something with update(); the rest it probes with hasattr.
    editor = types.SimpleNamespace(
        state=types.SimpleNamespace(things=[], brushes=[]),
        view_3d=QWidget())
    widget = PropertyEditor(editor)
    yield widget
    widget.deleteLater()


def representation_combo(panel, thing):
    # current_object is what update_object_prop writes through; the editor sets
    # it when the selection changes.
    panel.current_object = thing
    panel.populate_for_thing(thing)
    combo = panel._widgets.get('prop_render_mode_combo')
    assert combo is not None, "the Representation combo is not in the panel"
    return combo


# ---------------------------------------------------------------------------
# The entity's side of the contract
# ---------------------------------------------------------------------------

def test_the_default_model_is_a_real_asset():
    assert Prop.DEFAULT_MODEL_PATH
    assert os.path.isfile(Prop.DEFAULT_MODEL_PATH), (
        "Prop.DEFAULT_MODEL_PATH points at %r, which is not in the tree"
        % Prop.DEFAULT_MODEL_PATH)


def test_the_default_model_is_not_applied_to_every_prop():
    """Model collision is built from ``model_path`` regardless of render_mode.

    A default mesh on every Prop would give every billboard one barrel-shaped
    collision, and would make the implied render mode read as 'model'.
    """
    assert 'model_path' not in PROP_DEFAULTS
    fresh = Prop(pos=[0, 0, 0])
    assert not fresh.properties.get('model_path')
    assert fresh.properties['render_mode'] == 'billboard'


# ---------------------------------------------------------------------------
# The panel action
# ---------------------------------------------------------------------------

def test_choosing_model_gives_the_prop_the_default_mesh(panel):
    prop = Prop(pos=[0, 0, 0])
    combo = representation_combo(panel, prop)
    assert combo.currentText() == 'Billboard Sprite'

    combo.setCurrentText('Model')

    assert prop.properties['render_mode'] == 'model'
    assert prop.properties['model_path'] == Prop.DEFAULT_MODEL_PATH, (
        "the Prop was switched to Model with nothing to draw")


def test_an_authored_model_is_never_replaced(panel):
    prop = Prop(pos=[0, 0, 0],
                properties={'model_path': 'assets/models/prop_book.obj',
                            'render_mode': 'billboard'})
    combo = representation_combo(panel, prop)
    combo.setCurrentText('Model')

    assert prop.properties['model_path'] == 'assets/models/prop_book.obj', (
        "switching representation overwrote the author's own model")


def test_switching_back_to_billboard_adds_no_mesh(panel):
    prop = Prop(pos=[0, 0, 0])
    combo = representation_combo(panel, prop)
    combo.setCurrentText('Model')
    combo.setCurrentText('Billboard Sprite')

    assert prop.properties['render_mode'] == 'billboard'


def test_the_model_path_field_shows_the_default(panel):
    """The panel must not claim an empty path while the Prop has one."""
    from PyQt5.QtWidgets import QLineEdit

    prop = Prop(pos=[0, 0, 0])
    combo = representation_combo(panel, prop)
    combo.setCurrentText('Model')

    shown = [w.text() for w in panel.findChildren(QLineEdit)
             if w.text() == Prop.DEFAULT_MODEL_PATH]
    assert shown, (
        "the Model Path field is empty although the Prop now has %r"
        % Prop.DEFAULT_MODEL_PATH)


# ---------------------------------------------------------------------------
# A sprite Prop is not solid
# ---------------------------------------------------------------------------

def _collision_brushes_for(things):
    from editor.editor_state import EditorState
    from engine.logic_thread import LogicThread
    from engine.threaded_game_state import ThreadedGameState

    state = EditorState()
    state.brushes = []
    state.things = list(things)
    thread = LogicThread(ThreadedGameState(), state)
    return thread._build_model_collision_brushes()


def test_a_default_sprite_prop_has_no_collision():
    prop = Prop(pos=[0, 0, 0])
    assert prop.properties['render_mode'] == 'billboard'
    assert _collision_brushes_for([prop]) == []


def test_a_sprite_prop_that_still_has_a_mesh_path_has_no_collision():
    """The case the Representation switch makes reachable.

    Switching to Model gives the Prop the default mesh; switching back to
    Billboard leaves that path in place. It must not collide as a barrel while
    drawing a sprite.
    """
    prop = Prop(pos=[0, 0, 0], properties={
        'render_mode': 'billboard',
        'model_path': Prop.DEFAULT_MODEL_PATH,
        'no_collision': False,
        'physics_enabled': True,
    })
    assert _collision_brushes_for([prop]) == [], (
        "a Prop drawn as a sprite collided with the shape of a model it is "
        "not rendering")


def test_a_model_prop_does_collide():
    """The other half: the rule is about representation, not about Props."""
    prop = Prop(pos=[0, 0, 0], properties={
        'render_mode': 'model',
        'model_path': Prop.DEFAULT_MODEL_PATH,
        'no_collision': False,
    })
    assert _collision_brushes_for([prop]), (
        "a Prop drawn as a model should collide as one")


def test_an_authored_collision_box_still_applies_to_a_sprite_prop():
    """collision_size is authored for the entity, not derived from a mesh."""
    prop = Prop(pos=[0, 0, 0], properties={
        'render_mode': 'billboard',
        'model_path': Prop.DEFAULT_MODEL_PATH,
        'collision_size': [40.0, 40.0, 40.0],
        'no_collision': False,
    })
    brushes = _collision_brushes_for([prop])
    assert len(brushes) == 1
    assert brushes[0]['size'] == [40.0, 40.0, 40.0]
