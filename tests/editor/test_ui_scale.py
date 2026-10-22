"""Settings > Display > UI Scale: what ``[Display] ui_scale`` resolves to."""
import configparser

import pytest

pytestmark = pytest.mark.qt  # importing the editor package loads PyQt5

from editor import ui_scale


def _config(value=None):
    config = configparser.ConfigParser()
    config.add_section('Display')
    if value is not None:
        config.set('Display', 'ui_scale', value)
    return config


@pytest.mark.parametrize('value, expected', [
    ('auto', None), ('', None), ('AUTO', None), ('nonsense', None),
    ('85', 85), ('85%', 85), (' 110 ', 110), ('90.4', 90),
    ('10', ui_scale.MIN_UI_SCALE), ('900', ui_scale.MAX_UI_SCALE),
])
def test_parse(value, expected):
    assert ui_scale.parse_ui_scale(value) == expected


@pytest.mark.parametrize('screen, expected', [
    ((1470, 956 - 100), 75),     # MacBook Air 13" (M2/M3)
    ((1440, 900 - 100), 75),     # MacBook Air 13" (M1)
    ((1728, 1117 - 100), 90),    # MacBook Pro 16"
    ((1920, 1080), 100),         # the reference desktop itself
    ((2560, 1340), 100),         # never enlarges
    ((1024, 668), 65),           # never below the floor
    (None, 100), ((0, 0), 100),  # screen unknown
])
def test_auto_fits_the_reference_desktop(screen, expected):
    assert ui_scale.auto_ui_scale(screen) == expected


def test_auto_is_100_percent_off_macos():
    assert ui_scale.resolve_ui_scale('auto', platform='win32') == 1.0
    assert ui_scale.resolve_ui_scale('auto', platform='linux') == 1.0


def test_auto_on_macos_uses_the_screen():
    assert ui_scale.resolve_ui_scale(
        'auto', platform='darwin', screen_size=(1470, 856)) == 0.75


def test_a_percentage_applies_on_every_platform():
    assert ui_scale.resolve_ui_scale('80', platform='win32') == 0.8
    assert ui_scale.resolve_ui_scale(
        '125', platform='darwin', screen_size=(1470, 856)) == 1.25


def test_apply_sets_the_per_screen_factor(monkeypatch):
    monkeypatch.delenv('QT_SCREEN_SCALE_FACTORS', raising=False)
    monkeypatch.delenv('QT_SCALE_FACTOR', raising=False)
    assert ui_scale.apply_ui_scale(_config('80')) == 0.8
    # Per screen, so point-sized fonts are not scaled with the controls.
    import os
    assert os.environ['QT_SCREEN_SCALE_FACTORS'] == '0.8'
    assert 'QT_SCALE_FACTOR' not in os.environ


def test_apply_sets_nothing_at_100_percent(monkeypatch):
    monkeypatch.delenv('QT_SCREEN_SCALE_FACTORS', raising=False)
    monkeypatch.delenv('QT_SCALE_FACTOR', raising=False)
    assert ui_scale.apply_ui_scale(_config('100')) == 1.0
    import os
    assert 'QT_SCREEN_SCALE_FACTORS' not in os.environ


@pytest.mark.parametrize('variable', ['QT_SCREEN_SCALE_FACTORS', 'QT_SCALE_FACTOR'])
def test_an_environment_override_wins(monkeypatch, variable):
    monkeypatch.delenv('QT_SCREEN_SCALE_FACTORS', raising=False)
    monkeypatch.delenv('QT_SCALE_FACTOR', raising=False)
    monkeypatch.setenv(variable, '1.5')
    ui_scale.apply_ui_scale(_config('80'))
    import os
    assert os.environ[variable] == '1.5'
    other = ({'QT_SCREEN_SCALE_FACTORS', 'QT_SCALE_FACTOR'} - {variable}).pop()
    assert other not in os.environ
