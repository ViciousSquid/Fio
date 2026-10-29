"""Export _SHOWCASE through the desktop editor and its package dialog.

Only the destination file chooser is automated. The real level loader, editor
state, export dialog and package exporter execute normally. Settings are isolated
in a temporary working directory.
"""
import argparse
import os
from pathlib import Path
import tempfile
from unittest.mock import patch


def export_showcase(output, root=None):
    from PyQt5.QtWidgets import QApplication, QFileDialog
    from editor.main_window import MainWindow
    from editor.package_exporter import PackageExporter

    root = Path(root or Path(__file__).resolve().parents[2]).resolve()
    output = Path(output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    old_directory = Path.cwd()
    application = QApplication.instance() or QApplication([])
    errors = []
    original_export = PackageExporter.export

    def checked_export(*args, **kwargs):
        result = original_export(*args, **kwargs)
        errors.extend(result[1])
        return result

    with tempfile.TemporaryDirectory(prefix='fio-desktop-export-') as working:
        os.chdir(working)
        window = None
        try:
            window = MainWindow(str(root))
            if not window.load_level_file(str(root / 'maps/_SHOWCASE.json')):
                raise RuntimeError('Desktop Fio could not load _SHOWCASE')
            window.unsaved_changes = False
            window.export_game_package()
            dialog = window._export_dialog
            dialog.title_edit.setText('Fio _SHOWCASE')
            dialog.author_edit.setText('ViciousSquid')
            dialog.desc_edit.setPlainText('Desktop editor export for Android acceptance testing')
            with patch.object(QFileDialog, 'getSaveFileName', return_value=(str(output), 'Game Packages (*.fiopak)')), \
                 patch.object(PackageExporter, 'export', checked_export):
                dialog.export_btn.click()
            if 'Export successful!' not in dialog.dep_label.text() or errors:
                raise RuntimeError(f'Export failed or reported missing assets: {errors or dialog.dep_label.text()}')
            print(f'PASS: desktop Fio loaded _SHOWCASE and exported {output} ({output.stat().st_size} bytes)')
        finally:
            if window is not None:
                # This command runs without QApplication.exec_(). Merely
                # calling processEvents() does not deliver DeferredDelete
                # events, leaving the OpenGL viewport and its Qt children
                # alive until interpreter teardown (an intermittent native
                # crash on the offscreen Linux runner).
                from PyQt5.QtCore import QEvent
                import gc

                window._close_current_overlay()
                window.unsaved_changes = False
                window.close()
                window.deleteLater()
                application.processEvents()
                application.sendPostedEvents(None, QEvent.DeferredDelete)
                gc.collect()
                application.sendPostedEvents(None, QEvent.DeferredDelete)
            os.chdir(old_directory)
    return output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output')
    args = parser.parse_args(argv)
    export_showcase(args.output)


if __name__ == '__main__':
    main()
