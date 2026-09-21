"""Build a ``.fiopak`` from a repo map without the editor (stdlib only).

The editor's ``PackageExporter`` needs PyQt5; this is a headless equivalent for
CI and quick tests. It reads a map JSON, discovers the textures / models /
sounds it references, finds those assets anywhere under ``assets/``, and writes a
package the player's :class:`~player.fiopak.FioPackage` can open.

Usage::

    python -m player.tools.make_pak maps/Simple_Map_Test.json -o player/game.fiopak
    python -m player.tools.make_pak maps/DevTest.json --title "Dev Test"
"""

from __future__ import annotations

import argparse
import json
import os
import zipfile
from typing import Dict, Set


_ASSET_EXTS = {
    "textures": (".png", ".jpg", ".jpeg", ".tga", ".bmp"),
    "models": (".obj", ".glb", ".gltf"),
    "sounds": (".wav", ".ogg", ".mp3"),
}


def _scan_asset_names(node, out: Set[str]) -> None:
    """Recursively collect any string that looks like an asset filename."""
    if isinstance(node, dict):
        for v in node.values():
            _scan_asset_names(v, out)
    elif isinstance(node, list):
        for v in node:
            _scan_asset_names(v, out)
    elif isinstance(node, str):
        lower = node.lower()
        for exts in _ASSET_EXTS.values():
            if lower.endswith(exts):
                out.add(node)
                break


def _index_assets(assets_dir: str) -> Dict[str, str]:
    """Map basename -> absolute path for every file under assets/."""
    index: Dict[str, str] = {}
    for root, _dirs, files in os.walk(assets_dir):
        for name in files:
            index.setdefault(name, os.path.join(root, name))
    return index


def build_pak(map_path: str, output: str, root: str, title: str) -> "tuple[int, list]":
    map_path = os.path.abspath(map_path)
    root = os.path.abspath(root)
    with open(map_path, "r", encoding="utf-8") as f:
        map_data = json.load(f)

    referenced: Set[str] = set()
    _scan_asset_names(map_data, referenced)

    assets_dir = os.path.join(root, "assets")
    asset_index = _index_assets(assets_dir) if os.path.isdir(assets_dir) else {}

    map_basename = os.path.basename(map_path)
    archive_map = f"maps/{map_basename}"
    metadata = {"title": title, "author": "make_pak", "map_path": archive_map}

    warnings = []
    written = 0
    os.makedirs(os.path.dirname(os.path.abspath(output)) or ".", exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("metadata.json", json.dumps(metadata, indent=2))
        zf.write(map_path, archive_map)
        for ref in sorted(referenced):
            base = os.path.basename(ref.replace("\\", "/"))
            src = asset_index.get(base)
            if src is None:
                warnings.append(f"missing asset: {ref}")
                continue
            # File it under assets/<kind>/<basename> using its extension.
            kind = next(
                (k for k, exts in _ASSET_EXTS.items() if base.lower().endswith(exts)),
                "textures",
            )
            zf.write(src, f"assets/{kind}/{base}")
            written += 1
    return written, warnings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="Build a .fiopak from a map (no Qt).")
    p.add_argument("map", help="path to a map .json (e.g. maps/DevTest.json)")
    p.add_argument("-o", "--output", default="player/game.fiopak")
    p.add_argument("--root", default=".", help="repo root (for resolving assets/)")
    p.add_argument("--title", default=None)
    args = p.parse_args(argv)

    title = args.title or os.path.splitext(os.path.basename(args.map))[0]
    written, warnings = build_pak(args.map, args.output, args.root, title)
    print(f"[make_pak] wrote {args.output}: 1 map + {written} asset(s)")

    # If the map uses plugin entities, bundle those plugins (code + assets) into
    # the package so it is self-contained. Guarded: if the plugin system isn't
    # importable this is simply skipped. Bundling supplies the plugin's own
    # assets, so drop any "missing asset" warnings the base scan raised for them.
    try:
        import sys as _sys
        if os.path.abspath(args.root) not in _sys.path:
            _sys.path.insert(0, os.path.abspath(args.root))
        from plugins.manager import load_plugins
        from plugins.packaging import augment_fiopak
        load_plugins()
        summary = augment_fiopak(args.output)
        added = {a.lower() for a in summary.get("added_paths", set())}
        if summary.get("plugins"):
            print(f"[make_pak] bundled plugin(s): {', '.join(summary['plugins'])}")
            warnings = [w for w in warnings
                        if not any(w.lower().endswith(a) or a.endswith(
                            w.split('missing asset:')[-1].strip().lower())
                            for a in added)]
    except Exception:
        pass  # plugin system unavailable — plain package, no bundling.

    for w in warnings:
        print(f"[make_pak] warning: {w}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
