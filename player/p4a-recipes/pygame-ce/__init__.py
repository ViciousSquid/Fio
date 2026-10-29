"""Build the pinned pygame-ce SDL2 extensions for Android, not the host.

Without a recipe python-for-android's pip fallback installs a manylinux
x86-64 wheel even for an ARM64 APK. Those binaries cannot run on Android.

Derived from p4a's pygame (SDL2) recipe; keep the exact desktop pygame-ce
version and the p4a/NDK-provided SDL dependency libraries.
"""

from os.path import join

from pythonforandroid.recipe import CompiledComponentsPythonRecipe
from pythonforandroid.toolchain import current_directory


class PygameCERecipe(CompiledComponentsPythonRecipe):
    name = "pygame-ce"
    version = "2.5.8"
    url = "https://github.com/pygame-community/pygame-ce/archive/refs/tags/{version}.tar.gz"
    site_packages_name = "pygame"

    depends = ["sdl2", "sdl2_image", "sdl2_mixer", "sdl2_ttf",
               "setuptools", "jpeg", "png"]
    call_hostpython_via_targetpython = False
    install_in_hostpython = False

    def prebuild_arch(self, arch):
        super().prebuild_arch(arch)
        with current_directory(self.get_build_dir(arch.arch)):
            with open(join("buildconfig", "Setup.Android.SDL2.in")) as source:
                template = source.read()

            png = self.get_recipe("png", self.ctx)
            png_lib = join(png.get_build_dir(arch.arch), ".libs")
            png_include = png.get_build_dir(arch)
            jpeg = self.get_recipe("jpeg", self.ctx)
            jpeg_include = jpeg_lib = jpeg.get_build_dir(arch.arch)

            def include_dirs(name):
                recipe = self.get_recipe(name, self.ctx)
                return " ".join("-I" + p for p in recipe.get_include_dirs(arch))

            config = template.format(
                sdl_includes=(
                    " -I" + join(self.ctx.bootstrap.build_dir, "jni", "SDL", "include")
                    + " -L" + join(self.ctx.bootstrap.build_dir, "libs", str(arch))
                    + " -L" + png_lib + " -L" + jpeg_lib
                    + " -L" + arch.ndk_lib_dir_versioned
                ),
                sdl_ttf_includes="-I" + join(
                    self.ctx.bootstrap.build_dir, "jni", "SDL2_ttf"),
                sdl_image_includes=include_dirs("sdl2_image"),
                sdl_mixer_includes=include_dirs("sdl2_mixer"),
                jpeg_includes="-I" + jpeg_include,
                png_includes="-I" + png_include,
                freetype_includes="",
            )
            with open("Setup", "w") as output:
                output.write(config)

    def get_recipe_env(self, arch):
        env = super().get_recipe_env(arch)
        env["USE_SDL2"] = "1"
        env["PYGAME_CROSS_COMPILE"] = "TRUE"
        env["PYGAME_ANDROID"] = "TRUE"
        return env


recipe = PygameCERecipe()
