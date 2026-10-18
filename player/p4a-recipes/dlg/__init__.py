"""Header/source dependency at FreeType 2.14.1's exact dlg submodule commit."""
from pythonforandroid.recipe import Recipe


class DlgRecipe(Recipe):
    version = '395ccad2c1e0daae535c4d20bb0a3f2424648e17'
    url = 'https://github.com/nyorain/dlg/archive/{version}.tar.gz'
    sha256sum = '263a4a3777d9839e34cd41db988310659e0c00eab37beb7aedd30511a460133b'


recipe = DlgRecipe()
