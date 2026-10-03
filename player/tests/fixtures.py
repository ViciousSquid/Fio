"""Build in-memory ``.fiopak`` fixtures for tests (stdlib only).

A real package is just a ZIP; these helpers assemble one in memory so tests do
not depend on the editor or on any exported binary. When the repository's
``maps/`` directory is available, :func:`build_from_repo_map` packages a real
map so the loader is exercised against genuine editor output.
"""

from __future__ import annotations

import io
import json
import os
import zipfile
from typing import Dict, Optional


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def build_package(
    metadata: Dict,
    maps: Dict[str, Dict],
    assets: Optional[Dict[str, bytes]] = None,
    manifest_name: str = "metadata.json",
) -> bytes:
    """Assemble a ``.fiopak`` byte blob.

    ``maps`` maps an archive path (e.g. ``"maps/level.json"``) to map data;
    ``assets`` maps an archive path to raw bytes.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(manifest_name, json.dumps(metadata, indent=2))
        for path, data in maps.items():
            zf.writestr(path, json.dumps(data))
        for path, blob in (assets or {}).items():
            zf.writestr(path, blob)
    return buf.getvalue()


def minimal_package() -> bytes:
    """A tiny but complete package: one map + one texture."""
    metadata = {
        "title": "Test Game",
        "author": "tests",
        "map_path": "maps/level.json",
    }
    level = {
        "version": 3,
        "brushes": [{"pos": [0, 0, 0], "size": [100, 10, 100], "textures": {}, "id": 1}],
        "things": [
            {"type": "PlayerStart", "pos": [0.0, 32.0, 0.0], "properties": {}},
            {"type": "light", "pos": [0, 100, 0], "properties": {"radius": 400}},
        ],
    }
    assets = {"assets/textures/floor.png": b"\x89PNG\r\n\x1a\n-not-a-real-png-"}
    return build_package(metadata, {"maps/level.json": level}, assets)


def build_from_repo_map(map_name: str = "DevTest.json") -> Optional[bytes]:
    """Package a genuine repository map, or ``None`` if it isn't present."""
    src = os.path.join(REPO_ROOT, "maps", map_name)
    if not os.path.isfile(src):
        return None
    with open(src, "r", encoding="utf-8") as f:
        level = json.load(f)
    metadata = {"title": map_name, "map_path": f"maps/{map_name}"}
    return build_package(metadata, {f"maps/{map_name}": level})
