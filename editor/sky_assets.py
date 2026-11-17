"""Portable project asset handling for custom sky panoramas.

Kept independent of Qt so path handling and image import remain testable in
headless editor and packaging jobs.
"""

import filecmp
import os
import shutil


def store_sky_image_in_project(path, project_root=None):
    """Import a selected panorama into assets/backgrounds.

    Files already in that folder are referenced in place. Other selections are
    copied without overwriting a different asset with the same filename.
    Identical content is reused. The result is always project-relative.
    """
    raw_path = str(path or "").strip()
    if not raw_path:
        raise FileNotFoundError("No sky image selected")
    source = os.path.abspath(raw_path)
    if not os.path.isfile(source):
        raise FileNotFoundError(source)

    root = os.path.abspath(
        os.fspath(project_root) if project_root is not None
        else os.path.join(os.path.dirname(__file__), os.pardir)
    )
    backgrounds_dir = os.path.join(root, "assets", "backgrounds")
    source_real = os.path.realpath(source)
    backgrounds_real = os.path.realpath(backgrounds_dir)
    try:
        if os.path.commonpath((source_real, backgrounds_real)) == backgrounds_real:
            return os.path.relpath(source_real, root).replace("\\", "/")
    except ValueError:
        # Different Windows drives: import the image into this project.
        pass

    os.makedirs(backgrounds_dir, exist_ok=True)
    filename = os.path.basename(source)
    stem, extension = os.path.splitext(filename)
    destination = os.path.join(backgrounds_dir, filename)
    suffix = 1
    while os.path.exists(destination):
        try:
            if filecmp.cmp(source, destination, shallow=False):
                return os.path.relpath(destination, root).replace("\\", "/")
        except OSError:
            pass
        destination = os.path.join(backgrounds_dir, f"{stem}_{suffix}{extension}")
        suffix += 1
    shutil.copy2(source, destination)
    return os.path.relpath(destination, root).replace("\\", "/")


