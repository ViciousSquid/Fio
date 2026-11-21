"""Package export runs on a worker thread, from a scene fixed when Export
was pressed.

It ran on the GUI thread: reading, compressing and writing every map and
asset froze the editor until it was done. The scene to package is still
taken on the GUI thread (written to a copy of its own), and the result is
reported when the worker finishes.
"""

import json
import threading

import pytest

pytest.importorskip("PyQt5", reason="the export overlay is editor-tier")

from tests.helpers.worlds import box_brush  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture
def export(main_window, tmp_path, monkeypatch, qt_app):
    from PyQt5.QtWidgets import QFileDialog
    import editor.package_exporter as package_exporter

    monkeypatch.setattr(main_window, "root_dir", str(tmp_path))
    main_window.state.brushes.append(box_brush("wall", (0, 0, 0)))
    main_window.unsaved_changes = True
    toasts = []
    monkeypatch.setattr(main_window, "show_toast", lambda message, is_error=False, **_kw:
                        toasts.append((str(message), is_error)))
    out = tmp_path / "packages" / "game.fiopak"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(out), "")))

    gate = threading.Event()
    seen = {}

    def fake_export(self, output_path, metadata, map_path, parent_widget=None):
        seen['thread'] = threading.current_thread() is threading.main_thread()
        seen['parent'] = parent_widget
        with open(map_path, encoding='utf-8') as handle:
            seen['map'] = json.load(handle)
        assert gate.wait(10)
        if seen.get('fail'):
            return False, ["Missing asset: rock.png"]
        return True, []
    monkeypatch.setattr(package_exporter.PackageExporter, "export", fake_export)

    main_window.export_game_package()
    dialog = main_window._export_dialog
    dialog.title_edit.setText("Game")
    yield main_window, dialog, gate, seen, toasts, qt_app
    gate.set()
    job = getattr(main_window, '_export_job', None)
    if job is not None:
        job.wait(10)
    qt_app.processEvents()
    main_window._close_current_overlay()
    main_window.unsaved_changes = False


def test_export_runs_on_a_worker_from_the_scene_at_the_click(export, tmp_path):
    window, dialog, gate, seen, toasts, qt_app = export
    window._run_export(dialog, window._export_temp_file)
    job = window._export_job
    assert job is not None and job.running, "the export blocked the GUI thread"
    assert not dialog.export_btn.isEnabled()
    window.state.brushes[0]["name"] = "renamed after Export"
    gate.set()
    assert job.wait(10)
    qt_app.processEvents()
    assert seen['thread'] is False and seen['parent'] is None
    assert [b["name"] for b in seen['map']["brushes"]] == ["wall"]
    assert "Export successful" in dialog.dep_label.text()
    assert toasts[-1] == ("Package exported: game.fiopak", False)
    # The job's copy of the scene is gone; the overlay's own is the overlay's.
    leftovers = {p.name for p in (tmp_path / "maps").glob("export_temp_*.json")}
    assert leftovers == {__import__('os').path.basename(window._export_temp_file)}


def test_a_failed_export_is_reported_when_it_finishes(export):
    window, dialog, gate, seen, toasts, qt_app = export
    seen['fail'] = True
    window._run_export(dialog, window._export_temp_file)
    gate.set()
    assert window._export_job.wait(10)
    qt_app.processEvents()
    assert "Export failed" in dialog.dep_label.text()
    assert dialog.export_btn.isEnabled()
    assert toasts[-1][1] is True and "Missing asset" in toasts[-1][0]
