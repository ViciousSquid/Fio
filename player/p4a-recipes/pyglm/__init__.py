"""Build the engine's real GLM binding for Android, including libc++ support."""
from pythonforandroid.recipe import CppCompiledComponentsPythonRecipe


class PyGLMRecipe(CppCompiledComponentsPythonRecipe):
    version = "2.8.3"
    url = "https://files.pythonhosted.org/packages/41/8b/bdaf7b9cacecd28f7b4c6fc2d7d136824c506ad38cfdb37a05ea7ec88694/pyglm-{version}.tar.gz"
    sha256sum = "161781ea4d1267f796b645f85ebff53aeb8ee4f13b4e993c04d64c96d286e534"
    depends = ["setuptools"]
    site_packages_name = "glm"
    call_hostpython_via_targetpython = False


recipe = PyGLMRecipe()
