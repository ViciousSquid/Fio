"""Fio-pinned pygame-ce recipe for python-for-android.

The desktop and Android baselines both use pygame-ce 2.5.8. Keep this recipe
local so an upstream p4a recipe version cannot silently drift away from Fio's
runtime contract.
"""

from os.path import join
from pathlib import Path
import re

from pythonforandroid.recipe import CompiledComponentsPythonRecipe
from pythonforandroid.toolchain import current_directory


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

    def get_recipe_env(self, arch, **kwargs):
        # prebuild_arch's local dictionary does not reach the build subprocess.
        env = super().get_recipe_env(arch, **kwargs)
        env.update(USE_SDL2="1", PYGAME_CROSS_COMPILE="TRUE", PYGAME_ANDROID="TRUE",
                   ANDROID_ROOT=join(self.ctx.ndk.sysroot, "usr"))
        return env

    def prebuild_arch(self, arch):
        super().prebuild_arch(arch)
        with current_directory(self.get_build_dir(arch.arch)):
            # pygame-ce 2.5.8 defaults to Meson, whose own Android branch
            # explicitly rejects the platform. Its maintained Android Setup
            # template still uses setuptools. Use that backend for pip's
            # install step as well as our setup.py build_ext step.
            project = Path('pyproject.toml')
            text = project.read_text()
            text = re.sub(r'\[build-system\].*?(?=\n\[)',
                          '[build-system]\nrequires = ["setuptools>=77", "wheel", "Cython<3.2"]\n'
                          'build-backend = "setuptools.build_meta"\n', text, count=1, flags=re.S)
            project.write_text(text)
            setup_path = Path('setup.py')
            text = setup_path.read_text()
            text = text.replace('os.path.abspath(sys.argv[0])', 'os.path.abspath(__file__)')
            marker = 'import buildconfig.get_version as pg_ver'
            if '# Fio Android setuptools backend' not in text:
                text = text.replace(marker,
                    '# Fio Android setuptools backend\nimport os, sys\n'
                    'sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))\n' + marker, 1)
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
