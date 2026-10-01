"""Player glasses styles and Settings > Appearance.

Every style lives in ``assets/sprites/glasses/`` on the same transparent
canvas as the original pair, so any of them fills the billboard the same way.
Player 1 picks theirs on the Appearance tab (settings.ini ``[Appearance]
glasses``); player 2 always wears the classic pair.
"""

import configparser
import os
import sys

import numpy as np
import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, ROOT)

from engine.glasses import (  # noqa: E402
    DEFAULT_GLASSES, DEFAULT_SPRITE_KEY, GLASSES_STYLES, glasses_path,
    glasses_sprite_key, normalize_glasses,
)


def _image(style):
    Image = pytest.importorskip("PIL.Image")
    return Image.open(os.path.join(ROOT, glasses_path(style)))


@pytest.mark.parametrize("style", [s for s, _label, _f in GLASSES_STYLES])
def test_every_style_matches_the_classic_canvas_and_is_transparent(style):
    image = _image(style)
    classic = _image(DEFAULT_GLASSES)
    assert image.size == classic.size == (300, 128)
    assert image.mode == 'RGBA'
    alpha = np.asarray(image)[..., 3]
    # Trimmed to the frame, with transparent space above and below...
    assert alpha[0].max() == 0 and alpha[-1].max() == 0
    # ...and the frame itself is opaque.
    assert (alpha == 255).sum() > 0.05 * alpha.size


def test_the_classic_pair_is_the_default_and_player_twos():
    assert DEFAULT_GLASSES == GLASSES_STYLES[0][0] == 'classic'
    assert glasses_sprite_key(DEFAULT_GLASSES) == DEFAULT_SPRITE_KEY == 'Glasses'


def test_unknown_styles_fall_back_to_the_default():
    assert normalize_glasses(' Pixel_Shades ') == 'pixel_shades'
    assert normalize_glasses('monocle') == DEFAULT_GLASSES
    assert normalize_glasses(None) == DEFAULT_GLASSES
    assert glasses_sprite_key('monocle') == 'Glasses'
    assert glasses_sprite_key('cateye_pink') == 'Glasses:cateye_pink'


# ────────────────────────────
# Settings > Appearance
# ────────────────────────────

@pytest.fixture(scope="module")
def qt_app():
    pytest.importorskip("PyQt5", reason="Qt is not available in this environment")
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt5.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def _settings(config):
    from editor.SettingsWindow import SettingsWindow
    cwd = os.getcwd()
    os.chdir(ROOT)              # thumbnails load from assets/ like the app
    try:
        return SettingsWindow(config)
    finally:
        os.chdir(cwd)


def _appearance_tab(dialog):
    for i in range(dialog.tabs.count()):
        if dialog.tabs.tabText(i) == 'Appearance':
            return dialog.tabs.widget(i)
    return None


@pytest.mark.qt
def test_the_appearance_tab_lists_every_pair_with_a_thumbnail(qt_app):
    dialog = _settings(configparser.ConfigParser())
    assert _appearance_tab(dialog) is not None
    buttons = dialog.glasses_buttons.buttons()
    assert [b.property('glasses_style') for b in buttons] == \
        [s for s, _label, _f in GLASSES_STYLES]
    assert all(not b.icon().isNull() for b in buttons)
    # Nothing saved yet: the classic pair is picked.
    assert dialog.selected_glasses() == DEFAULT_GLASSES


@pytest.mark.qt
def test_the_choice_round_trips_through_settings_ini(qt_app):
    config = configparser.ConfigParser()
    config.add_section('Appearance')
    config.set('Appearance', 'glasses', 'sunnies_green')
    dialog = _settings(config)
    assert dialog.selected_glasses() == 'sunnies_green'

    dialog._glasses_button_for['pixel_shades'].click()
    dialog._save_settings()
    assert config.get('Appearance', 'glasses') == 'pixel_shades'


@pytest.mark.qt
def test_there_is_no_global_water_quality_or_portal_mirror_setting(qt_app):
    config = configparser.ConfigParser()
    config.add_section('Renderer')
    config.set('Renderer', 'water_quality', 'cheap')      # an old settings.ini
    dialog = _settings(config)
    assert not hasattr(dialog, 'water_quality_combo')
    dialog._save_settings()
    assert not config.has_option('Renderer', 'water_quality')
    assert not config.has_option('Display', 'portal_mirror')
