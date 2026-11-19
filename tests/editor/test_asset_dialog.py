"""Browse buttons choose from Fio's filesystem, packed assets included.

The model Browse button used the system file dialog, which cannot see inside
a mounted ``.fiopak``: a model packed in one could be placed from the Asset
Browser but never chosen again. editor/asset_dialog.py lists loose and packed
files together.
"""

import os
import zipfile

import pytest

pytestmark = pytest.mark.qt

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def test_the_filter_names_the_suffixes():
    from editor.asset_dialog import suffixes_from_filter
    assert suffixes_from_filter("Model Files (*.obj *.glb)") == ('.obj', '.glb')
    assert suffixes_from_filter("") == ()


@pytest.fixture
def packed_model(tmp_path, monkeypatch):
    """A project whose only model is packed in a user .fiopak."""
    from engine import asset_archives
    (tmp_path / "assets" / "models").mkdir(parents=True)
    (tmp_path / "assets" / "models" / "loose.obj").write_text("v 0 0 0\n")
    with zipfile.ZipFile(tmp_path / "assets" / "extra_pack.fiopak", "w") as pack:
        pack.writestr("models/packed_chair.obj", "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 3\n")
    monkeypatch.chdir(tmp_path)
    asset_archives.prepare_asset_archives(str(tmp_path))
    yield tmp_path
    monkeypatch.chdir(ROOT)
    asset_archives.prepare_asset_archives(ROOT)


def _files(dialog):
    out = {}
    stack = [dialog.tree.topLevelItem(0)]
    while stack:
        item = stack.pop()
        for i in range(item.childCount()):
            child = item.child(i)
            stack.append(child)
            if not child.data(0, dialog_dir_role()):
                out[child.data(0, dialog_path_role())] = child.text(0)
    return out


def dialog_dir_role():
    from editor.asset_dialog import _DIR_ROLE
    return _DIR_ROLE


def dialog_path_role():
    from editor.asset_dialog import _PATH_ROLE
    return _PATH_ROLE


def test_a_packed_model_is_listed_and_marked_with_its_pack(qt_app, packed_model):
    from editor.asset_dialog import AssetFileDialog
    dialog = AssetFileDialog(None, "Select Model", "assets/models",
                             "Model Files (*.obj *.glb)")
    files = _files(dialog)
    assert "assets/models/packed_chair.obj" in files
    assert "extra_pack.fiopak" in files["assets/models/packed_chair.obj"]
    assert files["assets/models/loose.obj"] == "loose.obj"
    # The tree opens on the folder asked for.
    assert dialog.tree.currentItem().data(0, dialog_path_role()) == "assets/models"

    dialog.select_path("assets/models/packed_chair.obj")
    assert dialog.selected_path == "assets/models/packed_chair.obj"
    dialog.deleteLater()


def test_files_of_other_kinds_are_left_out(qt_app, packed_model):
    (packed_model / "assets" / "models" / "notes.txt").write_text("x")
    from editor.asset_dialog import AssetFileDialog
    dialog = AssetFileDialog(None, "Select Model", "assets/models",
                             "Model Files (*.obj *.glb)")
    assert "assets/models/notes.txt" not in _files(dialog)
    dialog.deleteLater()
