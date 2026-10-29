"""SDL_image: fetch only dependencies enabled by its Android.mk.

Upstream's prebuild fetches every optional codec recursively, including JPEG XL
and its Skia submodule, although Android.mk disables those codecs. PNG/JPEG use
the bundled stb decoder; WebP is the sole enabled external dependency.
"""
from pathlib import Path

import sh
from pythonforandroid.logger import shprint
from pythonforandroid.recipe import BootstrapNDKRecipe


class SDLImageRecipe(BootstrapNDKRecipe):
    version = '2.8.2'
    url = 'https://github.com/libsdl-org/SDL_image/releases/download/release-{version}/SDL2_image-{version}.tar.gz'
    dir_name = 'SDL2_image'

    def get_include_dirs(self, arch):
        return [str(Path(self.ctx.bootstrap.build_dir) / 'jni/SDL2_image/include')]

    def prebuild_arch(self, arch):
        directory = Path(self.get_build_dir(arch.arch))
        webp = directory / 'external/libwebp'
        if not webp.is_dir() or not any(webp.iterdir()):
            shprint(sh.git, 'clone', '--depth', '1', '-b', '1.0.3-SDL',
                    'https://github.com/libsdl-org/libwebp.git', str(webp))
        makefile = directory / 'Android.mk'
        source = makefile.read_text()
        source = source.replace('SUPPORT_WEBP ?= false', 'SUPPORT_WEBP := true')
        makefile.write_text(source)
        super().prebuild_arch(arch)


recipe = SDLImageRecipe()
