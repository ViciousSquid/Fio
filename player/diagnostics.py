"""Persistent Python crash reports with an Android-native export action."""
from __future__ import annotations

import platform
import os
import traceback
from pathlib import Path


def device_validation_requested():
    """Enable pose reports only in a debuggable APK with an explicit intent."""
    from .main import is_android
    if not is_android():
        return False
    from jnius import autoclass
    activity = autoclass('org.kivy.android.PythonActivity').mActivity
    return bool(activity.getApplicationInfo().flags & 2) and bool(
        activity.getIntent().getBooleanExtra('fio_validation', False))


def report_path():
    from .main import is_android
    if is_android():
        private = os.environ.get('ANDROID_PRIVATE') or os.environ.get('ANDROID_ARGUMENT')
        if private and Path(private).is_dir():
            return Path(private) / 'fio-player-report.txt'
        from jnius import autoclass
        activity = autoclass("org.kivy.android.PythonActivity").mActivity
        return Path(str(activity.getFilesDir().getAbsolutePath())) / "fio-player-report.txt"
    return Path.home() / ".fio-player-report.txt"


def record_error(exc):
    from . import __version__
    report = f"Fio Player {__version__}\nPython: {platform.python_version()}\nPlatform: {platform.platform()}\nArchitecture: {platform.machine()}\n"
    try:
        report += f"Page size: {os.sysconf('SC_PAGE_SIZE')}\n"
    except (AttributeError, OSError, ValueError):
        pass
    from .main import is_android
    if is_android():
        try:
            from jnius import autoclass
            build = autoclass('android.os.Build')
            version = autoclass('android.os.Build$VERSION')
            report += f"Device: {build.MANUFACTURER} {build.MODEL}\nAndroid: {version.RELEASE} (API {version.SDK_INT})\n"
        except Exception:
            pass
    try:
        from OpenGL import GL
        for name, token in (('GL version', GL.GL_VERSION), ('GL vendor', GL.GL_VENDOR), ('GL renderer', GL.GL_RENDERER)):
            value = GL.glGetString(token)
            if value:
                report += f"{name}: {value.decode('utf-8', 'replace')}\n"
    except Exception:
        pass
    report += '\n'
    report += "".join(traceback.format_exception(exc))
    print(report)
    try:
        report_path().write_text(report, encoding="utf-8")
    except (OSError, ImportError):
        pass
    return report


def show_error(exc, documents=None):
    report = record_error(exc)
    if documents is not None:
        from android.runnable import run_on_ui_thread
        from jnius import autoclass, PythonJavaClass, java_method

        class Listener(PythonJavaClass):
            __javainterfaces__ = ["android/content/DialogInterface$OnClickListener"]
            __javacontext__ = "app"

            @java_method("(Landroid/content/DialogInterface;I)V")
            def onClick(self, dialog, which):
                if which == -1:
                    documents.export_report(report)
                elif documents.fatal:
                    documents.finished = True
                    documents.activity.finish()

        # Keep the JNI callback alive until the dialog has been dismissed.
        documents.error_listener = Listener()

        @run_on_ui_thread
        def show():
            builder = autoclass("android.app.AlertDialog$Builder")(documents.activity)
            # Pyjnius needs an explicit Java String for CharSequence overloads.
            text = autoclass("java.lang.String")
            builder.setTitle(text("Fio Player error"))
            builder.setCancelable(False)
            builder.setMessage(text(str(exc)[:2000]))
            builder.setPositiveButton(text("Save error report"), documents.error_listener)
            builder.setNegativeButton(text("Close"), documents.error_listener)
            builder.show()
        show()
