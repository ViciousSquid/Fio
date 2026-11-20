"""CPU model loading from disk or from an asset source.

An asset source is anything with ``read_asset(path) -> bytes | None`` and
``read_text(path) -> str | None``: a played package (the standalone player's
``.fiopak``), read in place. Without one a model is read from disk. Either way
the result is a loaded :class:`~engine.glb_loader.GLBLoader` or
:class:`~engine.obj_loader.OBJLoader`, or None.
"""
from __future__ import annotations

from pathlib import Path


def load_model(path, assets=None):
    from .glb_loader import GLBLoader
    from .obj_loader import OBJLoader
    suffix = Path(path).suffix.lower()
    if suffix == '.obj':
        loader = OBJLoader()
        if assets:
            text = assets.read_text(path)
            return loader if text is not None and loader.load_text(text, path, assets.read_text) else None
        return loader if loader.load(path) else None
    if suffix in ('.glb', '.gltf'):
        loader = GLBLoader()
        if assets:
            raw = assets.read_asset(path)
            if raw is None:
                return None
            # A .gltf goes through load_bytes too: it used to be parsed here
            # directly, with no decode budget and no error handling, so a
            # malformed packaged .gltf raised into the caller.
            return loader if loader.load_bytes(raw, path, assets.read_asset) else None
        return loader if loader.load(path) else None
    return None
