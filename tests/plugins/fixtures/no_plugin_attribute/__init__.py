"""A package in the plugins directory that is not a plugin at all.

It exposes neither ``PLUGIN`` nor ``get_plugin()``, so the manager must skip it
with a message rather than raising.
"""

SOMETHING_ELSE = 42
