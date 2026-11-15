"""Importing a sky image creates a project-owned portable asset."""

from editor.sky_assets import store_sky_image_in_project


def test_selected_sky_image_is_copied_into_project_backgrounds(tmp_path):
    project = tmp_path / "project"
    source = tmp_path / "outside" / "nebula.jpg"
    source.parent.mkdir()
    source.write_bytes(b"panorama data")

    relative = store_sky_image_in_project(source, project)

    assert relative == "assets/backgrounds/nebula.jpg"
    assert (project / relative).read_bytes() == b"panorama data"


def test_selecting_same_external_sky_image_reuses_existing_copy(tmp_path):
    project = tmp_path / "project"
    source = tmp_path / "outside" / "nebula.webp"
    source.parent.mkdir()
    source.write_bytes(b"same panorama")
    backgrounds = project / "assets" / "backgrounds"
    backgrounds.mkdir(parents=True)
    (backgrounds / source.name).write_bytes(b"same panorama")

    relative = store_sky_image_in_project(source, project)

    assert relative == "assets/backgrounds/nebula.webp"
    assert sorted(path.name for path in backgrounds.iterdir()) == ["nebula.webp"]


def test_different_image_with_same_name_is_not_overwritten(tmp_path):
    project = tmp_path / "project"
    source = tmp_path / "outside" / "nebula.jpg"
    source.parent.mkdir()
    source.write_bytes(b"new panorama")
    backgrounds = project / "assets" / "backgrounds"
    backgrounds.mkdir(parents=True)
    (backgrounds / source.name).write_bytes(b"existing panorama")

    relative = store_sky_image_in_project(source, project)

    assert relative == "assets/backgrounds/nebula_1.jpg"
    assert (backgrounds / "nebula.jpg").read_bytes() == b"existing panorama"
    assert (backgrounds / "nebula_1.jpg").read_bytes() == b"new panorama"


