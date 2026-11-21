"""Run a package export off the GUI thread.

An export reads, compresses and writes every map and asset the world uses --
seconds on a big project -- and ran on the GUI thread, so the editor froze
until it was done (audit: package export on the UI thread).
"""

import threading

from PyQt5.QtCore import QObject, pyqtSignal


class _Relay(QObject):
    #: ``(success, errors)``. Emitted on the worker; queued to the GUI
    #: thread, where the relay lives.
    finished = pyqtSignal(bool, list)


class ExportJob:
    """One :meth:`editor.package_exporter.PackageExporter.export` on a worker.

    The job reads files only: whatever of the editor's state the package
    should hold is written to *map_path* on the GUI thread before the job
    starts, so the package is one consistent moment of the scene however the
    user goes on editing. *on_finished(success, errors)* is called on the GUI
    thread when it is done; *cleanup* (removing a temporary map) runs on the
    worker once the export no longer needs the file.
    """

    def __init__(self, exporter, output_path, metadata, map_path,
                 on_finished, cleanup=None):
        self._relay = _Relay()
        self._relay.finished.connect(on_finished)
        self._exporter = exporter
        self._args = (output_path, metadata, map_path)
        self._cleanup = cleanup
        self._thread = None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        self._thread = threading.Thread(target=self._run, name="fio-export",
                                        daemon=True)
        self._thread.start()

    def wait(self, timeout=None):
        """Block until the export has finished; False if *timeout* ran out."""
        if self._thread is None:
            return True
        self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self):
        output_path, metadata, map_path = self._args
        try:
            # No parent widget: a message box cannot be raised off the GUI
            # thread. The errors are reported when the job finishes.
            success, errors = self._exporter.export(output_path, metadata, map_path,
                                                    parent_widget=None)
        except Exception as e:                  # export() reports its own
            success, errors = False, [f"Export failed: {e}"]
        finally:
            if self._cleanup is not None:
                try:
                    self._cleanup()
                except Exception:
                    pass
        self._relay.finished.emit(bool(success), [str(e) for e in errors])
