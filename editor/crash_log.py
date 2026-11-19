"""A crash log on disk, for builds that have no console to print to.

The release builds are windowed: an exception before the first window, or one
the in-app console never got to show, left no trace anywhere. Every unhandled
exception is appended to ``fio_crash.log`` next to settings.ini (or in the
per-user data folder when the install is read-only).
"""

import datetime
import os
import sys
import traceback

LOG_NAME = 'fio_crash.log'
#: The log is a diagnostic, not an archive: started afresh past this size.
MAX_BYTES = 512 * 1024

_path = None


def log_path():
    return _path


def record(exc_type, exc, tb):
    """Append one exception to the crash log. Never raises."""
    if _path is None:
        return
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        mode = 'a'
        try:
            if os.path.getsize(_path) > MAX_BYTES:
                mode = 'w'
        except OSError:
            pass
        os.makedirs(os.path.dirname(_path) or '.', exist_ok=True)
        with open(_path, mode, encoding='utf-8') as handle:
            stamp = datetime.datetime.now().isoformat(timespec='seconds')
            handle.write(f"--- {stamp} ---\n{text}\n")
    except Exception:
        pass


def install(directory):
    """Log every unhandled exception under *directory*, then pass it on to
    whichever hook is installed (now or later: see :func:`wrap_current`)."""
    global _path
    _path = os.path.join(directory, LOG_NAME)
    wrap_current()
    return _path


def wrap_current():
    """Put the crash log in front of the current ``sys.excepthook``.

    Called again after the editor installs its own hook, which replaces
    whatever was there.
    """
    inner = sys.excepthook
    if getattr(inner, '_fio_crash_log', False):
        return

    def hook(exc_type, exc, tb):
        if not issubclass(exc_type, KeyboardInterrupt):
            record(exc_type, exc, tb)
        inner(exc_type, exc, tb)

    hook._fio_crash_log = True
    sys.excepthook = hook
