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


def _load_simd_patch_without_android_dependencies():
    """Execute only the pure helper; p4a is absent from desktop CI."""
    recipe = RECIPES / "pygame-ce" / "__init__.py"
    tree = ast.parse(recipe.read_text(encoding="utf-8"), filename=str(recipe))
    target = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "patch_pygame_simd_target")
    namespace = {}
    exec(compile(ast.Module(body=[target], type_ignores=[]), str(recipe), "exec"),
         namespace)
    return namespace["patch_pygame_simd_target"]


def test_android_pygame_simd_probe_uses_target_not_build_host():
    """Simulate pygame-ce's spawn override on an x86-64 CI machine."""
    from types import SimpleNamespace

    patch = _load_simd_patch_without_android_dependencies()
    recipe = (RECIPES / "pygame-ce" / "__init__.py").read_text()
    assert "patch_pygame_simd_target(text, arch.arch)" in recipe

    # This is the relevant upstream pygame-ce 2.5.8 branch: it selects
    # -mavx2 according to platform.machine(), not the cross-compiler.
    source = """
def spawn(self, cmd):
    machine_name = platform.machine()
    if (machine_name.startswith(("x86", "i686")) or
            machine_name.lower() == "amd64"):
        if (self.compiler_type == "unix"
                and any(item.endswith("simd_blitters_avx2.o") for item in cmd)):
            cmd.append("-mavx2")
    return cmd
"""
    def flags(target):
        code = patch(source, target)
        namespace = {"platform": SimpleNamespace(machine=lambda: "x86_64")}
        exec(code, namespace)
        compiler = SimpleNamespace(compiler_type="unix")
        return namespace["spawn"](compiler, ["clang", "-c", "simd_blitters_avx2.o"])

    # Host is x86-64 in both cases. Only Android target ABI may enable AVX2.
    assert "-mavx2" not in flags("arm64-v8a")
    assert "-mavx2" in flags("x86_64")


def test_android_pygame_simd_patch_fails_closed_on_upstream_changes():
    import pytest

    patch = _load_simd_patch_without_android_dependencies()
    with pytest.raises(RuntimeError, match="SIMD probe changed"):
        patch("some updated setup.py", "arm64-v8a")
    with pytest.raises(RuntimeError, match="SIMD probe changed"):
        patch("machine_name = platform.machine()\nmachine_name = platform.machine()",
              "arm64-v8a")
    with pytest.raises(ValueError, match="Unsupported Android"):
        patch("machine_name = platform.machine()", "unsupported-target")


def _load_install_helper_without_android_dependencies():
    """Load the pure staging helper; no p4a/NDK is required to test it."""
    import shutil
    import struct
    from tempfile import TemporaryDirectory

    recipe = RECIPES / "pygame-ce" / "__init__.py"
    tree = ast.parse(recipe.read_text(encoding="utf-8"), filename=str(recipe))
    target = next(node for node in tree.body
                  if isinstance(node, ast.FunctionDef)
                  and node.name == "install_cross_built_pygame")
    namespace = {"Path": Path, "shutil": shutil, "struct": struct,
                 "TemporaryDirectory": TemporaryDirectory}
    exec(compile(ast.Module(body=[target], type_ignores=[]),
                 str(recipe), "exec"), namespace)
    return namespace["install_cross_built_pygame"]


def _synthetic_android_elf(machine):
    """Minimal ELF64 header sufficient for the target-architecture gate."""
    import struct

    header = bytearray(64)
    header[:6] = b"\x7fELF\x02\x01"
    struct.pack_into("<HH", header, 16, 3, machine)
    return bytes(header)


def test_android_pygame_install_bypasses_the_second_build():
    """Build_ext output and source files reach the target without pip/Meson."""
    import tempfile
    from pathlib import Path

    import pytest

    recipe = (RECIPES / "pygame-ce" / "__init__.py").read_text()
    tree = ast.parse(recipe)
    cls = next(x for x in tree.body
               if isinstance(x, ast.ClassDef) and x.name == "PygameCeRecipe")
    method = next(x for x in cls.body
                  if isinstance(x, ast.FunctionDef)
                  and x.name == "install_python_package")
    calls = [n.func.id for n in ast.walk(method)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
    assert "install_cross_built_pygame" in calls
    assert "install_python_package" not in calls
    assert not any(isinstance(n, ast.Attribute) and n.attr == "pip"
                   for n in ast.walk(method))

    stage = _load_install_helper_without_android_dependencies()
    for target, machine in (("arm64-v8a", 183), ("x86_64", 62)):
        with tempfile.TemporaryDirectory() as work:
            root = Path(work) / "source"
            src = root / "src_py"
            (src / "_sdl2").mkdir(parents=True)
            (src / "__init__.py").write_text("from . import base\n")
            (src / "_sdl2" / "__init__.py").write_text("")
            (src / "freesansbold.ttf").write_bytes(b"font")
            built = root / "build" / "lib.linux-x86_64-cpython-314" / "pygame"
            built.mkdir(parents=True)
            # The extension suffix can name the build host before p4a
            # normalises it, but the ELF itself must match the Android ABI.
            extension = built / "base.cpython-314-x86_64-linux-gnu.so"
            extension.write_bytes(_synthetic_android_elf(machine))
            destination = Path(work) / "site-packages"
            (destination / "pygame").mkdir(parents=True)
            (destination / "pygame" / "stale.py").write_text("obsolete")

            assert stage(root, destination, target) == 1
            installed = destination / "pygame"
            assert (installed / "__init__.py").read_text() == "from . import base\n"
            assert (installed / "_sdl2" / "__init__.py").is_file()
            assert (installed / "freesansbold.ttf").read_bytes() == b"font"
            assert (installed / extension.name).read_bytes() == _synthetic_android_elf(machine)
            assert not (installed / "stale.py").exists()

            # A wrongly-built native extension must never reach the package.
            extension.write_bytes(_synthetic_android_elf(62 if machine == 183 else 183))
            with pytest.raises(RuntimeError, match="non-"):
                stage(root, destination, target)
            assert (installed / extension.name).read_bytes() == _synthetic_android_elf(machine)


def test_android_pygame_install_refuses_missing_or_unsupported_builds():
    import tempfile

    import pytest

    stage = _load_install_helper_without_android_dependencies()
    with tempfile.TemporaryDirectory() as work:
        root = Path(work) / "source"
        dest = Path(work) / "site"
        (root / "src_py").mkdir(parents=True)
        (root / "src_py" / "__init__.py").write_text("")
        with pytest.raises(RuntimeError, match="unique pygame build"):
            stage(root, dest, "arm64-v8a")
        with pytest.raises(ValueError, match="Unsupported Android"):
            stage(root, dest, "armeabi-v7a")
