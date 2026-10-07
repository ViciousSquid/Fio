"""An example plugin that adds a deferred renderer to Fio.

Copy this package into ``plugins/`` and start Fio; ``r_renderer`` then lists
``Deferred`` and ``r_renderer Deferred`` switches to it live. See the wiki's
Renderer Development Guide for a walk through the code.
"""

from plugins.api import FioPlugin


def _create_deferred_renderer(config):
    # Imported when the host creates the renderer -- with its GL context
    # current -- so loading the plugin needs no OpenGL.
    from .renderer import DeferredRenderer
    return DeferredRenderer(config)


class DeferredRendererPlugin(FioPlugin):
    name = "deferred_renderer"
    version = "1.0.0"
    description = "Example: a deferred renderer (r_renderer Deferred)"
    api_version = "1.6.0"          # the Renderer protocol contract

    def register(self, api):
        api.register_renderer("Deferred", _create_deferred_renderer)


PLUGIN = DeferredRendererPlugin()
