"""Portable asset discovery and packaging for map-authored sky images."""

import json
import zipfile

from editor.package_exporter import PackageExporter
from engine.package_assets import ProjectAssets, map_assets


def test_map_assets_collects_player_start_sky_image():
    level = {
        "things": [{
            "type": "playerstart",
            "properties": {"sky_image": "assets/backgrounds/space.webp"},
        }]
    }
    assert map_assets(level) == {"assets/backgrounds/space.webp"}


def test_project_assets_resolves_bare_background_filename(tmp_path):
    image = tmp_path / "assets" / "backgrounds" / "nebula.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"panorama data")

    with ProjectAssets(tmp_path) as assets:
        assert assets.resolve("nebula.jpg") == "assets/backgrounds/nebula.jpg"


def test_package_export_carries_player_start_sky_image(tmp_path):
    sky_path = "assets/backgrounds/space.webp"
    sky_data = b"test panorama bytes"
    sky_file = tmp_path / sky_path
    sky_file.parent.mkdir(parents=True)
    sky_file.write_bytes(sky_data)

    map_file = tmp_path / "maps" / "world.json"
    map_file.parent.mkdir()
    map_file.write_text(json.dumps({
        "version": 1,
        "things": [{
            "type": "playerstart",
            "pos": [0, 0, 0],
            "properties": {
                "type": "playerstart",
                "primary": True,
                "sky_image": sky_path,
            },
        }],
    }), encoding="utf-8")

    package_file = tmp_path / "world.fiopak"
    exporter = PackageExporter(None, str(tmp_path))
    success, errors = exporter.export(
        str(package_file), {"title": "World"}, str(map_file)
    )

    assert success, errors
    with zipfile.ZipFile(package_file) as package:
        assert package.read(sky_path) == sky_data
        packaged_map = json.loads(package.read("maps/world.json"))
    assert packaged_map["things"][0]["properties"]["sky_image"] == sky_path


