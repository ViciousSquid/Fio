"""Fio-pinned pygame-ce recipe for python-for-android.

The desktop and Android baselines both use pygame-ce 2.5.8. Keep this recipe
local so an upstream p4a recipe version cannot silently drift away from Fio's
runtime contract.
"""

from os.path import join
from pathlib import Path
import shutil
import struct
from tempfile import TemporaryDirectory

from pythonforandroid.recipe import CompiledComponentsPythonRecipe
from pythonforandroid.toolchain import current_directory


def patch_pygame_simd_target(setup_text: str, target_arch: str) -> str:
    """Make pygame-ce's AVX2 selection use the Android target, not CI host.

    pygame-ce 2.5.8 replaces CCompiler.spawn and uses platform.machine()
    to decide whether to append -mavx2 to its SIMD object compilation.
    The setup.py runs under an x86-64 host Python even for arm64-v8a.
    Preserve the upstream fallback/stub sources, but never enable AVX2
    instructions unless the *target* ABI is x86-64.
    """
    if target_arch not in ("arm64-v8a", "x86_64"):
        raise ValueError(f"Unsupported Android pygame-ce ABI: {target_arch}")
    probe = "machine_name = platform.machine()"
    target_probe = f"machine_name = {target_arch!r}"
    # Buildozer reuses each architecture's extracted source tree for its
    # debug and release builds. Reapplying the same target patch is safe.
    if setup_text.count(target_probe) == 1 and probe not in setup_text:
        return setup_text
    if setup_text.count(probe) != 1 or target_probe in setup_text:
        raise RuntimeError(
            "pygame-ce 2.5.8 SIMD probe changed; update Android cross-build patch"
        )
    return setup_text.replace(probe, target_probe, 1)


def install_cross_built_pygame(build_root: Path, install_root: Path,
                               target_arch: str) -> int:
    """Stage the already cross-compiled extensions without pip or Meson.

    CompiledComponentsPythonRecipe.build_arch has already run setup.py
    build_ext. Its normal install_python_package subsequently runs pip install
    ., which can select pygame-ce's incompatible Meson Android backend and
    rebuild for the build host. Copy the actual build_ext output instead.
    """
    machines = {"arm64-v8a": 183, "x86_64": 62}
    if target_arch not in machines:
        raise ValueError(f"Unsupported Android pygame-ce ABI: {target_arch}")

    source = build_root / "src_py"
    candidates = sorted(
        child / "pygame" for child in (build_root / "build").glob("lib.*")
        if (child / "pygame").is_dir()
    )
    if not (source / "__init__.py").is_file() or len(candidates) != 1:
        raise RuntimeError("pygame-ce build_ext did not produce a unique pygame build")
    compiled = candidates[0]
    libraries = sorted(compiled.rglob("*.so"))
    if not libraries or not any(path.name.split(".")[0] == "base" for path in libraries):
        raise RuntimeError("pygame-ce cross-build is missing its base extension")
    for library in libraries:
        with library.open("rb") as file:
            header = file.read(20)
        if (len(header) < 20 or header[:6] != b"\x7fELF\x02\x01"
                or struct.unpack_from("<H", header, 18)[0] != machines[target_arch]):
            raise RuntimeError(
                f"pygame-ce produced a non-{target_arch} native extension: {library}"
            )

    # src_py contains the runtime Python modules, font and icons. build_ext
    # places the native extension modules under build/lib.*/pygame, potentially
    # using host-Python ABI suffixes; p4a normalises those when bundling.
    # Stage before replacing a previous installation so failure cannot leave
    # an incomplete Python package. There is deliberately no pip invocation.
    install_root.mkdir(parents=True, exist_ok=True)
    destination = install_root / "pygame"
    if destination.is_symlink():
        raise RuntimeError("Refusing to overwrite a symlinked pygame target")
    with TemporaryDirectory(prefix=".fio-pygame-", dir=install_root) as staging:
        staged = Path(staging) / "pygame"
        shutil.copytree(source, staged)
        shutil.copytree(compiled, staged, dirs_exist_ok=True)
        if destination.exists():
            shutil.rmtree(destination)
        shutil.move(str(staged), str(destination))
    return len(libraries)


class PygameCeRecipe(CompiledComponentsPythonRecipe):
    version = "2.5.8"
    url = "https://github.com/pygame-community/pygame-ce/archive/refs/tags/{version}.tar.gz"

    site_packages_name = "pygame"
    name = "pygame-ce"

    depends = [
        "sdl2",
        "sdl2_image",
        "sdl2_mixer",
        "sdl2_ttf",
        "setuptools",
        "jpeg",
        "png",
    ]
    call_hostpython_via_targetpython = False
    install_in_hostpython = False
    hostpython_prerequisites = ["setuptools", "Cython<3.2"]

    def install_python_package(self, arch, name=None, env=None, is_dir=True):
        """Install cross-compiled output, never invoke pip/mesonpy again."""
        install_cross_built_pygame(
            Path(self.get_build_dir(arch.arch)),
            Path(self.ctx.get_python_install_dir(arch.arch)),
            arch.arch,
        )

    def get_recipe_env(self, arch, **kwargs):
        # prebuild_arch's local dictionary does not reach the build subprocess.
        env = super().get_recipe_env(arch, **kwargs)
        env.update(USE_SDL2="1", PYGAME_CROSS_COMPILE="TRUE", PYGAME_ANDROID="TRUE",
                   ANDROID_ROOT=join(self.ctx.ndk.sysroot, "usr"))
        return env

    def prebuild_arch(self, arch):
        super().prebuild_arch(arch)
        with current_directory(self.get_build_dir(arch.arch)):
            # Build only through pygame-ce's Android Setup + setup.py.
            # install_python_package below stages build_ext results directly;
            # never run pip on pygame-ce's pyproject.toml/Meson backend.
            setup_path = Path('setup.py')
            text = setup_path.read_text()
            text = text.replace('os.path.abspath(sys.argv[0])', 'os.path.abspath(__file__)')
            marker = 'import buildconfig.get_version as pg_ver'
            if '# Fio Android setuptools backend' not in text:
                text = text.replace(marker,
                    '# Fio Android setuptools backend\nimport os, sys\n'
                    'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n' + marker, 1)
            text = patch_pygame_simd_target(text, arch.arch)
            setup_path.write_text(text)
            setup_template = open(
                join("buildconfig", "Setup.Android.SDL2.in"),
                encoding="utf-8",
            ).read()

            png = self.get_recipe("png", self.ctx)
            png_lib_dir = join(png.get_build_dir(arch.arch), ".libs")
            png_inc_dir = png.get_build_dir(arch)

            jpeg = self.get_recipe("jpeg", self.ctx)
            jpeg_inc_dir = jpeg_lib_dir = jpeg.get_build_dir(arch.arch)

            sdl_mixer_includes = ""
            sdl2_mixer_recipe = self.get_recipe("sdl2_mixer", self.ctx)
            for include_dir in sdl2_mixer_recipe.get_include_dirs(arch):
                sdl_mixer_includes += f"-I{include_dir} "
            sdl_image_includes = " ".join(
                f"-I{directory}" for directory in
                self.get_recipe("sdl2_image", self.ctx).get_include_dirs(arch)
            )

            setup_file = setup_template.format(
                sdl_includes=(
                    " -I"
                    + join(self.ctx.bootstrap.build_dir, "jni", "SDL", "include")
                    + " -L"
                    + join(self.ctx.bootstrap.build_dir, "libs", str(arch))
                    + " -L"
                    + png_lib_dir
                    + " -L"
                    + jpeg_lib_dir
                    + " -L"
                    + arch.ndk_lib_dir_versioned
                ),
                sdl_ttf_includes="-I"
                + join(self.ctx.bootstrap.build_dir, "jni", "SDL2_ttf"),
                sdl_image_includes=sdl_image_includes,
                sdl_mixer_includes=sdl_mixer_includes,
                jpeg_includes="-I" + jpeg_inc_dir,
                png_includes="-I" + png_inc_dir,
                freetype_includes="",
            )
            with open("Setup", "w", encoding="utf-8") as setup:
                setup.write(setup_file)




recipe = PygameCeRecipe()
