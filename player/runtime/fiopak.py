"""The shared module ``engine.fiopak``, under the name the player has always used.

The package reader and asset collector serve the desktop editor (export, Play
Game Package) as well as the player, so they live in the engine: a desktop
build does not include the player package.
"""
import sys

from engine import fiopak as _shared

sys.modules[__name__] = _shared
