"""Cross-compile PyGLM against Android Python and the NDK C++ runtime.

The native extension must never come from a Linux host wheel. Based on the
upstream python-for-android PyGLM recipe (#3353), pinned to Fio's baseline.
"""

from pythonforandroid.recipe import PyProjectRecipe


class PyGLMRecipe(PyProjectRecipe):
    name = "pyglm"
    version = "2.8.3"
    url = "https://github.com/Zuzu-Typ/PyGLM/archive/refs/tags/{version}.tar.gz"


recipe = PyGLMRecipe()
