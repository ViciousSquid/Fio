"""Settings > Display > UI Scale: what ``[Display] ui_scale`` resolves to."""
import configparser
import os

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
    ('inf', None), ('1e309', None), ('nan', None),
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


@pytest.fixture
def clean_env(monkeypatch):
    for name in ('QT_SCREEN_SCALE_FACTORS', 'QT_SCALE_FACTOR', ui_scale._SET_BY_FIO):
        monkeypatch.delenv(name, raising=False)
    return os.environ


def test_a_percentage_applies_to_every_screen(clean_env):
    assert ui_scale.apply_ui_scale(_config('80')) == 0.8
    # Per screen, so point-sized fonts are not scaled with the controls.
    assert clean_env['QT_SCREEN_SCALE_FACTORS'].split(';') == \
        ['0.8'] * ui_scale._MAX_SCREENS
    assert 'QT_SCALE_FACTOR' not in clean_env


def test_auto_applies_to_the_main_screen_only(clean_env, monkeypatch):
    monkeypatch.setattr(ui_scale.sys, 'platform', 'darwin')
    monkeypatch.setattr(ui_scale, '_mac_main_screen_points', lambda: (1470, 856))
    assert ui_scale.apply_ui_scale(_config('auto')) == 0.75
    assert clean_env['QT_SCREEN_SCALE_FACTORS'] == '0.75'


def test_a_percent_sign_in_settings_ini_is_read(clean_env):
    config = configparser.ConfigParser()  # interpolating, as main.py's is
    config.read_string('[Display]\nui_scale = 85%\n')
    assert ui_scale.read_ui_scale_setting(config) == '85%'
    assert ui_scale.apply_ui_scale(config) == 0.85


def test_apply_sets_nothing_at_100_percent(clean_env):
    assert ui_scale.apply_ui_scale(_config('100')) == 1.0
    assert 'QT_SCREEN_SCALE_FACTORS' not in clean_env


@pytest.mark.parametrize('variable', ['QT_SCREEN_SCALE_FACTORS', 'QT_SCALE_FACTOR'])
def test_an_environment_override_wins(clean_env, monkeypatch, variable):
    monkeypatch.setenv(variable, '1.5')
    ui_scale.apply_ui_scale(_config('80'))
    assert clean_env[variable] == '1.5'
    other = ({'QT_SCREEN_SCALE_FACTORS', 'QT_SCALE_FACTOR'} - {variable}).pop()
    assert other not in clean_env


def test_a_restart_applies_the_new_setting(clean_env):
    """Apply & Restart inherits the factor the previous run set."""
    ui_scale.apply_ui_scale(_config('80'))
    ui_scale.apply_ui_scale(_config('125'))  # the restarted process
    assert clean_env['QT_SCREEN_SCALE_FACTORS'].split(';')[0] == '1.25'
    ui_scale.apply_ui_scale(_config('100'))
    assert 'QT_SCREEN_SCALE_FACTORS' not in clean_env


def test_a_restart_keeps_a_users_override(clean_env, monkeypatch):
    monkeypatch.setenv('QT_SCREEN_SCALE_FACTORS', '1.5')
    ui_scale.apply_ui_scale(_config('80'))
    ui_scale.apply_ui_scale(_config('80'))  # restarted, override inherited
    assert clean_env['QT_SCREEN_SCALE_FACTORS'] == '1.5'
