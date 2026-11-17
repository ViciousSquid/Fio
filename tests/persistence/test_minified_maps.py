"""File > Save As Minified...: a map saved as minified JSON is the same map
in a smaller file, stays minified on Save, and is read wherever a map is."""
import json
import zipfile

import pytest

pytestmark = pytest.mark.qt


def _scene(window):
    window.state.brushes.extend([
        {"name": "floor", "pos": [0, -8, 0], "size": [512, 16, 512]},
        {"name": "wall", "pos": [0, 64, -200], "size": [512, 128, 16],
         "brush_group": "walls"},
    ])
    window.update_all_ui()


def _save_as(window, monkeypatch, path, minified):
    monkeypatch.setattr(window, "_ask_save_path", lambda start: (str(path), minified))
    window.save_level_as()


def test_the_file_menu_offers_save_as_minified(main_window):
    labels = [a.text().replace("&", "") for a in main_window.file_menu.actions()]
    assert "Save As Minified..." in labels
    assert labels.index("Save As Minified...") == labels.index("Save As...") + 1


def test_save_as_minified_starts_the_dialog_on_minified(main_window, monkeypatch):
    asked = []
    monkeypatch.setattr(main_window, "_ask_save_path",
                        lambda start: asked.append(start) or ("", start))
    main_window.save_level_as_minified()
    main_window.save_level_as()
    assert asked == [True, False]


def test_a_minified_map_is_one_line_and_the_same_map(main_window, monkeypatch, tmp_path):
    window = main_window
    _scene(window)
    plain, small = tmp_path / "plain.json", tmp_path / "small.json"

    _save_as(window, monkeypatch, plain, False)
    _save_as(window, monkeypatch, small, True)

    text = small.read_text(encoding="utf-8")
    assert "\n" not in text and ": " not in text and ", " not in text
    assert json.loads(text) == json.loads(plain.read_text(encoding="utf-8"))
    assert len(text) < len(plain.read_text(encoding="utf-8")) * 0.8
    assert window.map_minified


def test_save_keeps_a_minified_map_minified(main_window, monkeypatch, tmp_path):
    window = main_window
    _scene(window)
    path = tmp_path / "small.json"
    _save_as(window, monkeypatch, path, True)
    window.state.brushes.append({"name": "added", "pos": [9, 9, 9], "size": [8, 8, 8]})

    window.save_level()

    text = path.read_text(encoding="utf-8")
    assert "\n" not in text and '"added"' in text


def test_a_minified_map_opens_and_stays_minified(main_window, monkeypatch, tmp_path):
    window = main_window
    _scene(window)
    small, plain = tmp_path / "small.json", tmp_path / "plain.json"
    _save_as(window, monkeypatch, small, True)
    _save_as(window, monkeypatch, plain, False)
    assert not window.map_minified

    assert window.load_level_file(str(small))
    assert window.map_minified
    assert [b.get("name") for b in window.state.brushes] == ["floor", "wall"]
    window.save_level()
    assert "\n" not in small.read_text(encoding="utf-8")

    assert window.load_level_file(str(plain))
    assert not window.map_minified
    window.save_level()
    assert "\n" in plain.read_text(encoding="utf-8")


def test_a_new_map_is_not_minified(main_window, monkeypatch, tmp_path):
    window = main_window
    _scene(window)
    _save_as(window, monkeypatch, tmp_path / "small.json", True)
    window.unsaved_changes = False

    window.new_map()

    assert not window.map_minified


def test_a_minified_map_keeps_its_terrain_stamps_beside_it(
        main_window, monkeypatch, tmp_path):
    from PyQt5.QtGui import QColor, QImage
    from engine.terrain import Terrain

    image = QImage(16, 16, QImage.Format_RGBA8888)
    image.fill(QColor(120, 110, 90, 255))
    texture = str(tmp_path / "path.png")
    assert image.save(texture)
    window = main_window
    terrain = Terrain(seed=5)
    terrain.add_paint_stamp(100.0, 40.0, 64.0, texture)
    window.terrain = terrain
    window.state.terrain_data = terrain.to_dict()
    path = tmp_path / "small.json"

    _save_as(window, monkeypatch, path, True)

    assert "paint_stamps" not in json.loads(path.read_text())["terrain_data"]
    assert (tmp_path / "small.paint").exists()
    assert window.load_level_file(str(path))
    assert len(window.terrain.paint_stamps) == 1 and window.map_minified
    window.unsaved_changes = False


def test_a_minified_map_is_packaged_minified(main_window, monkeypatch, tmp_path):
    from editor.package_exporter import PackageExporter
    from engine.fiopak import FioPackage

    window = main_window
    _scene(window)
    project = tmp_path / "project"
    (project / "maps").mkdir(parents=True)
    path = project / "maps" / "start.json"
    _save_as(window, monkeypatch, path, True)
    out = tmp_path / "game.fiopak"

    ok, errors = PackageExporter(None, str(project)).export(
        str(out), {"title": "T"}, str(path))

    assert ok, errors
    with zipfile.ZipFile(out) as zf:
        packaged = zf.read("maps/start.json").decode("utf-8")
    assert "\n" not in packaged.strip()
    assert [b.get("name") for b in json.loads(packaged)["brushes"]] == ["floor", "wall"]
    assert FioPackage is not None
