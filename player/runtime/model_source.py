"""CPU model loading from disk or a universal package asset source."""
from __future__ import annotations

import json
from pathlib import Path


def load_model(path, assets=None):
    from engine.glb_loader import GLBLoader
    from engine.obj_loader import OBJLoader
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
            if suffix == '.gltf':
                loader._filepath_hint, loader._asset_reader = path, assets.read_asset
                loader.json_data = json.loads(raw)
                loader._parse_json_structure()
                return loader
            return loader if loader.load_bytes(raw, path, assets.read_asset) else None
        return loader if loader.load(path) else None
    return None
