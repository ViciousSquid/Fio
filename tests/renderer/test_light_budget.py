"""The dynamic-light budget and the low-power shader switch.

Two things that were written down in more than one place and had drifted apart:

* the renderer's light budget and the light array its shaders declared had
  previously drifted apart, so a scene with more lights than the shader array
  could index past the end of the array;
* "is this machine low-power?", detected separately by the renderer and the
  Settings window, and defaulting to *yes* on every machine regardless.
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

pytest.importorskip("OpenGL", reason="this module imports Fio's GL-backed modules; importing PyOpenGL needs no GPU, but it does need the package")

# The desktop dependency set (PyOpenGL here) has to be importable; no
# display and no GPU are needed.
pytestmark = pytest.mark.qt

from tests.helpers.paths import read_source  # noqa: E402

from engine import shaders  # noqa: E402


LIT_SHADERS = ('lit.frag', 'textured.frag')
LOW_POWER_SHADERS = ('lit_arm.frag', 'textured_arm.frag')


def _resolve(source, token):
    """A GLSL integer literal, or the value of the ``#define`` it names."""
    if token.isdigit():
        return int(token)
    match = re.search(r'#define\s+%s\s+(\d+)' % re.escape(token), source)
    assert match, "no #define for %s" % token
    return int(match.group(1))


def array_size(source):
    """The declared size of the shader's ``lights[]`` array."""
    match = re.search(r'uniform Light lights\[(\w+)\]', source)
    assert match, "shader declares no lights[] array"
    return _resolve(source, match.group(1))


def loop_bound(source):
    """The constant the light loop is clamped to, or None if it is unclamped."""
    match = re.search(r'for ?\(int i = 0; i < active_lights(?: && i < (\w+))?;',
                      source)
    assert match, "shader has no light loop"
    return _resolve(source, match.group(1)) if match.group(1) else None


@pytest.mark.parametrize("name", LIT_SHADERS + LOW_POWER_SHADERS +
                         ('water.frag', 'terrain.frag'))
def test_every_light_loop_is_clamped_to_its_own_array(name):
    """An unclamped loop reads past the array whenever active_lights is larger."""
    source = shaders.DEFAULT_SHADERS[name]
    assert loop_bound(source) == array_size(source)


@pytest.mark.parametrize("name", LIT_SHADERS)
def test_the_main_shaders_hold_the_full_budget(name):
    assert array_size(shaders.DEFAULT_SHADERS[name]) == shaders.MAX_LIGHTS


@pytest.mark.parametrize("name", LOW_POWER_SHADERS)
def test_the_low_power_shaders_keep_their_smaller_budget(name):
    assert array_size(shaders.DEFAULT_SHADERS[name]) == shaders.MAX_LIGHTS_ARM
    assert shaders.MAX_LIGHTS_ARM < shaders.MAX_LIGHTS


def test_the_renderer_budget_is_the_shader_capacity():
    from engine.renderer_core import BaseRenderer
    assert BaseRenderer.MAX_LIGHTS == shaders.MAX_LIGHTS


def test_water_and_terrain_are_capped_to_what_they_declare():
    from engine import renderer_core
    caps = renderer_core._SHADER_LIGHT_CAPS
    assert caps['water'] == array_size(shaders.DEFAULT_SHADERS['water.frag'])
    assert caps['terrain'] == array_size(shaders.DEFAULT_SHADERS['terrain.frag'])


def test_terrain_light_subset_uses_the_shader_constant():
    source = read_source('engine', 'renderer_core.py')
    assert 'max_terrain_lights = shaders.MAX_LIGHTS_TERRAIN' in source


def test_light_shader_sources_are_rewritten_to_the_shared_ubo():
    source = shaders.light_ubo_source(shaders.DEFAULT_SHADERS['lit.frag'])
    assert 'layout(std140) uniform FioLightBlock' in source
    assert 'uniform Light lights[' not in source
    assert '.position.xyz' in source
    assert '.params.x' in source
    assert 'int(lights[' in source


# ---------------------------------------------------------------------------
# Low-power detection
# ---------------------------------------------------------------------------

@pytest.fixture
def clean_env(monkeypatch):
    monkeypatch.delenv('FIO_ARM_MODE', raising=False)
    return monkeypatch


def test_x86_is_not_low_power(clean_env, monkeypatch):
    monkeypatch.setattr(shaders.platform, 'machine', lambda: 'x86_64')
    monkeypatch.setattr(shaders.sys, 'platform', 'linux')
    assert shaders.detect_low_power_arm()[0] is False


def test_apple_silicon_is_not_low_power(clean_env, monkeypatch):
    """aarch64, but a desktop-class GPU — it wants the full shaders."""
    monkeypatch.setattr(shaders.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(shaders.sys, 'platform', 'darwin')
    is_low, reason = shaders.detect_low_power_arm()
    assert is_low is False
    assert 'Apple' in reason


def test_a_snapdragon_8cx_is_low_power(clean_env, monkeypatch):
    """The Surface Pro X/9 class part this switch exists for."""
    monkeypatch.setattr(shaders.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(shaders.sys, 'platform', 'win32')
    monkeypatch.setattr(shaders, '_arm_cpu_name',
                        lambda: 'Microsoft SQ3 @ 3.00 GHz')
    assert shaders.detect_low_power_arm()[0] is True


def test_a_snapdragon_x_elite_is_not_low_power(clean_env, monkeypatch):
    monkeypatch.setattr(shaders.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(shaders.sys, 'platform', 'win32')
    monkeypatch.setattr(shaders, '_arm_cpu_name',
                        lambda: 'Snapdragon(R) X Elite - X1E80100')
    assert shaders.detect_low_power_arm()[0] is False


def test_an_unknown_arm_part_gets_the_conservative_answer(clean_env, monkeypatch):
    monkeypatch.setattr(shaders.platform, 'machine', lambda: 'aarch64')
    monkeypatch.setattr(shaders.sys, 'platform', 'linux')
    monkeypatch.setattr(shaders, '_arm_cpu_name', lambda: 'Some New Thing')
    assert shaders.detect_low_power_arm()[0] is True


@pytest.mark.parametrize("value,expected", [('1', True), ('0', False),
                                            ('true', True), ('off', False)])
def test_the_environment_can_force_the_answer(monkeypatch, value, expected):
    monkeypatch.setenv('FIO_ARM_MODE', value)
    assert shaders.detect_low_power_arm()[0] is expected


def test_the_renderer_and_the_settings_window_ask_the_same_question():
    """They used to detect this separately and could disagree."""
    renderer_src = read_source('engine', 'renderer_core.py')
    settings_src = read_source('editor', 'SettingsWindow.py')
    assert 'shaders.detect_low_power_arm()' in renderer_src
    assert 'detect_low_power_arm' in settings_src
    # Neither may still carry its own copy of the probe.
    assert "platform.machine().lower()" not in renderer_src
    assert "platform.machine().lower()" not in settings_src


def test_the_old_settings_key_is_still_honoured():
    """An existing settings.ini says arm_mode; it must keep working."""
    renderer_src = read_source('engine', 'renderer_core.py')
    assert "'arm_mode'" in renderer_src
    assert "'lowpower_mode'" in renderer_src


def test_light_ubo_cpu_layout_is_one_record_of_four_parallel_arrays():
    from engine.renderer_core import BaseRenderer
    renderer_src = read_source('engine', 'renderer_core.py')
    assert "('position', '<f4', (self.MAX_LIGHTS, 4))" in renderer_src
    assert "('color', '<f4', (self.MAX_LIGHTS, 4))" in renderer_src
    assert "('params', '<f4', (self.MAX_LIGHTS, 4))" in renderer_src
    assert "('indices', '<i4', (self.MAX_LIGHTS, 4))" in renderer_src
    assert BaseRenderer.MAX_LIGHTS == shaders.MAX_LIGHTS
