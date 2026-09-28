"""A model that cannot be read is a ``False``, never an exception.

``GLBLoader`` fell back to a ``ResourceManager.get_binary_asset`` that does not
exist and caught only ``ImportError``, so any GLB it could not open itself —
missing, unreadable, a directory — raised ``AttributeError`` out of ``load``
into the logic thread's collision builder.
"""

from engine.glb_loader import GLBLoader


def test_a_missing_glb_is_reported_not_raised(tmp_path):
    assert GLBLoader().load(str(tmp_path / "missing.glb")) is False


def test_an_unreadable_glb_is_reported_not_raised(tmp_path):
    path = tmp_path / "actually_a_directory.glb"
    path.mkdir()
    assert GLBLoader().load(str(path)) is False


def test_a_corrupt_glb_is_reported_not_raised(tmp_path):
    path = tmp_path / "corrupt.glb"
    path.write_bytes(b"not a glb at all")
    assert GLBLoader().load(str(path)) is False
