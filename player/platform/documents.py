"""Android Storage Access Framework; no broad storage permissions required.

Java callbacks only enqueue work. ZIP loading and GL uploads run on the SDL
thread, where the graphics context is current.
"""
from __future__ import annotations

import os
from pathlib import Path
from queue import SimpleQueue

MAX_PACKAGE_BYTES = 1024 * 1024 * 1024
OPEN_WORLD = 4101
SAVE_REPORT = 4102


def copy_package(source, destination, limit=MAX_PACKAGE_BYTES):
    """Copy a provider stream atomically with a bounded disk budget."""
    destination = Path(destination)
    temporary = destination.with_suffix(".partial")
    try:
        with temporary.open("wb") as output:
            remaining = limit
            while chunk := source.read(min(1024 * 1024, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ValueError("World package exceeds the 1 GiB import limit")
                output.write(chunk)
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return str(destination)


class AndroidDocuments:
    def __init__(self):
        from android import activity
        from jnius import autoclass

        self.activity = autoclass("org.kivy.android.PythonActivity").mActivity
        self.Intent = autoclass("android.content.Intent")
        self.events = SimpleQueue()
        self.report = ""
        self.finished = False
        self.fatal = False
        self._activity_module = activity
        activity.bind(on_activity_result=self._on_result)

    def close(self):
        if self._activity_module is not None:
            self._activity_module.unbind(on_activity_result=self._on_result)
            self._activity_module = None

    def _launch(self, intent, request):
        from android.runnable import run_on_ui_thread

        @run_on_ui_thread
        def launch():
            try:
                self.activity.startActivityForResult(intent, request)
            except Exception as exc:
                self.events.put(("error", str(exc)))
        launch()

    def open_world(self):
        intent = self.Intent(self.Intent.ACTION_OPEN_DOCUMENT)
        intent.addCategory(self.Intent.CATEGORY_OPENABLE)
        # File providers disagree about the MIME type for custom extensions.
        intent.setType("*/*")
        self._launch(intent, OPEN_WORLD)

    def export_report(self, report):
        self.report = report
        intent = self.Intent(self.Intent.ACTION_CREATE_DOCUMENT)
        intent.addCategory(self.Intent.CATEGORY_OPENABLE)
        intent.setType("text/plain")
        intent.putExtra(self.Intent.EXTRA_TITLE, "fio-player-report.txt")
        self._launch(intent, SAVE_REPORT)

    def _on_result(self, request, result, intent):
        if request not in (OPEN_WORLD, SAVE_REPORT) or result != -1 or intent is None:
            if request == SAVE_REPORT and self.fatal:
                self.finished = True
                self.activity.finish()
            return
        uri = intent.getData()
        if uri is not None:
            self.events.put(("open" if request == OPEN_WORLD else "report", uri))

    def drain(self, on_open, on_error):
        while not self.events.empty():
            kind, value = self.events.get()
            try:
                if kind == "error":
                    raise RuntimeError(value)
                mode = "r" if kind == "open" else "w"
                descriptor = self.activity.getContentResolver().openFileDescriptor(value, mode)
                if descriptor is None:
                    raise OSError("The document provider could not open this file")
                try:
                    with os.fdopen(os.dup(descriptor.getFd()), "rb" if mode == "r" else "wb") as stream:
                        if kind == "open":
                            directory = Path(str(self.activity.getFilesDir().getAbsolutePath()))
                            path = copy_package(stream, directory / "imported.fiopak")
                        else:
                            stream.write(self.report.encode("utf-8"))
                finally:
                    descriptor.close()
                if kind == "open":
                    on_open(path)
                elif self.fatal:
                    self.finished = True
                    self.activity.finish()
            except Exception as exc:
                on_error(exc)
