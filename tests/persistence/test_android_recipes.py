"""Prevent silently packaging manylinux wheels into the Android player.

The Android Buildozer specification points to this local recipe directory.
Without these recipes, p4a's pip fallback downloads host x86-64 extensions
for pygame-ce and PyGLM even when building an ARM64 APK.
"""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RECIPES = PROJECT_ROOT / "player" / "p4a-recipes"


def test_local_android_native_recipes_are_present():
    spec = (PROJECT_ROOT / "player" / "buildozer.spec").read_text()
    assert "p4a.local_recipes = ./p4a-recipes" in spec
    assert "pygame-ce==2.5.8" in spec
    assert "pyglm==2.8.3" in spec
    for name in ("dlg", "freetype", "openssl", "pygame-ce", "pyglm", "sdl2_image"):
        path = RECIPES / name / "__init__.py"
        assert path.is_file(), f"missing Android cross-compilation recipe: {name}"
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def test_android_native_pygame_and_glm_never_use_host_wheel_fallback():
    pygame = (RECIPES / "pygame-ce" / "__init__.py").read_text()
    glm = (RECIPES / "pyglm" / "__init__.py").read_text()
    assert 'name = "pygame-ce"' in pygame
    assert 'version = "2.5.8"' in pygame
    assert 'PYGAME_CROSS_COMPILE' in pygame
    assert 'CompiledComponentsPythonRecipe' in pygame
    assert 'version = "2.8.3"' in glm
    assert 'CppCompiledComponentsPythonRecipe' in glm
