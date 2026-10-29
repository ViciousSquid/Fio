"""Build the pinned upstream FreeType tag through its official GitHub mirror.

The GNU download host is restricted in the cloud build environment. Git tag
archives lack generated configure/dlg files, so bootstrap them with the same
dlg revision recorded by upstream, without modifying the application sources.
"""
from pathlib import Path
import shutil

import sh
from pythonforandroid.logger import shprint
from pythonforandroid.recipes.freetype import FreetypeRecipe as UpstreamFreetypeRecipe
from pythonforandroid.util import current_directory


class FreetypeRecipe(UpstreamFreetypeRecipe):
    version = '2.14.1'
    url = 'https://github.com/freetype/freetype/archive/refs/tags/VER-2-14-1.tar.gz'
    sha256sum = '44bd69d1f0750603410cfd4f26f1c5523c5a3a087a2dfa8639b757dc7c6677be'
    depends = [*UpstreamFreetypeRecipe.depends, 'dlg']

    def prebuild_arch(self, arch):
        super().prebuild_arch(arch)
        directory = Path(self.get_build_dir(arch.arch))
        dlg = Path(self.get_recipe('dlg', self.ctx).get_build_dir(arch.arch))
        (directory / 'include/dlg').mkdir(exist_ok=True)
        for filename in ('dlg.h', 'output.h'):
            shutil.copy2(dlg / 'include/dlg' / filename, directory / 'include/dlg' / filename)
        for source in (dlg / 'src/dlg').iterdir():
            if source.is_file():
                shutil.copy2(source, directory / 'src/dlg' / source.name)
        if not (directory / 'builds/unix/configure').is_file():
            with current_directory(str(directory)):
                shprint(sh.Command('./autogen.sh'), _env=self.get_recipe_env(arch))


recipe = FreetypeRecipe()
