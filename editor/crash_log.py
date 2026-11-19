"""A crash log on disk, for builds that have no console to print to.

The release builds are windowed: an exception before the first window, or one
the in-app console never got to show, left no trace anywhere. Every unhandled
exception is appended to ``fio_crash.log`` next to settings.ini (or in the
per-user data folder when the install is read-only).
"""

import datetime
import faulthandler
import os
import sys
import threading
import traceback

LOG_NAME = 'fio_crash.log'
#: Native crashes (a segfault in a GL driver or in Qt) end the process before
#: any Python hook runs; :mod:`faulthandler` writes every thread's stack here.
FAULT_LOG_NAME = 'fio_fault.log'
#: The log is a diagnostic, not an archive: past this size the current log is
#: moved to ``fio_crash.log.1`` (replacing the previous one) and a new one is
#: started, so the first traceback of a burst -- the root cause -- survives.
MAX_BYTES = 512 * 1024

_path = None
#: The last traceback written, and how many identical ones followed it. An
#: exception that repeats every frame is written once, not 60 times a second.
_last_text = None
_repeats = 0
_lock = threading.Lock()
#: The open fault log: faulthandler writes to its descriptor at crash time,
#: so it stays open for the life of the process.
_fault_file = None


def log_path():
    return _path


def _rotate_if_full():
    try:
        if os.path.getsize(_path) > MAX_BYTES:
            os.replace(_path, _path + '.1')
    except OSError:
        pass


def record(exc_type, exc, tb):
    """Append one exception to the crash log. Never raises."""
    global _last_text, _repeats
    if _path is None:
        return
    try:
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        with _lock:
            if text == _last_text:
                _repeats += 1
                return
            repeats, _repeats, _last_text = _repeats, 0, text
            _rotate_if_full()
            os.makedirs(os.path.dirname(_path) or '.', exist_ok=True)
            with open(_path, 'a', encoding='utf-8') as handle:
                if repeats:
                    handle.write(f"(the exception above repeated {repeats} more time(s))\n\n")
                stamp = datetime.datetime.now().isoformat(timespec='seconds')
                handle.write(f"--- {stamp} ---\n{text}\n")
    except Exception:
        pass


def _thread_hook(args):
    """Record an exception that ends a background thread, then let Python
    print it as usual. Without this a windowed build loses it entirely."""
    if args.exc_type is not None and not issubclass(args.exc_type, SystemExit):
        record(args.exc_type, args.exc_value, args.exc_traceback)
    threading.__excepthook__(args)


def install(directory):
    """Log every unhandled exception under *directory*, then pass it on to
    whichever hook is installed (now or later: see :func:`wrap_current`).
    Exceptions that end a background thread are logged too."""
    global _path
    _path = os.path.join(directory, LOG_NAME)
    wrap_current()
    if threading.excepthook is threading.__excepthook__:
        threading.excepthook = _thread_hook
    _install_fault_log(directory)
    return _path


def _install_fault_log(directory):
    """Send native-crash stacks to ``fio_fault.log`` under *directory*.

    Left alone when faulthandler is already on (``python -X faulthandler``,
    pytest): whoever enabled it chose where it writes. The log is started
    afresh once it outgrows :data:`MAX_BYTES`. Never raises.
    """
    global _fault_file
    if _fault_file is not None or faulthandler.is_enabled():
        return
    try:
        path = os.path.join(directory, FAULT_LOG_NAME)
        os.makedirs(directory or '.', exist_ok=True)
        try:
            if os.path.getsize(path) > MAX_BYTES:
                os.replace(path, path + '.1')
        except OSError:
            pass
        handle = open(path, 'a', encoding='utf-8')
        faulthandler.enable(file=handle, all_threads=True)
        _fault_file = handle
    except Exception:
        pass


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
