"""Crash-safe writes for Fio's documents (maps, autosaves, saved games, settings).

Opening the destination with ``open(path, "w")`` truncates it before a single
byte of the new document exists, so anything that fails while the document is
being produced — an entity that will not serialise, a full disk — leaves the
user's previous map or save empty.  Here the document is serialised first,
written beside the destination, and moved over it in one ``os.replace``: the
destination always holds either the old document or the complete new one.
"""

import json
import os
import secrets
from contextlib import contextmanager


@contextmanager
def atomic_output(path: str, *, binary=False, encoding="utf-8"):
    """Yield an owned sibling file, then sync and replace *path* on success.

    Exclusive creation gives overlapping saves their own files and leaves any
    pre-existing ``.tmp`` file or symlink alone. Use ordinary file permissions
    (``0666`` filtered by the process umask), just as ``open(path, "w")`` does.
    Only a temporary created by this call is ever removed on failure.
    """
    path = os.fspath(path)
    directory = os.path.dirname(os.path.abspath(path))
    for _ in range(100):
        temporary = os.path.join(directory, f".fio-{secrets.token_hex(8)}.tmp")
        try:
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o666)
        except FileExistsError:
            continue
        break
    else:
        raise FileExistsError("Could not create an exclusive temporary file")

    try:
        try:
            handle = os.fdopen(descriptor, "w+b" if binary else "w",
                               encoding=None if binary else encoding)
        except BaseException:
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise
        with handle:
            yield handle
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_json_atomic(path: str, data, **dump_kwargs) -> None:
    """Write *data* as JSON to *path*, replacing it only once fully written.

    ``dump_kwargs`` are passed to :func:`json.dumps` (``indent``,
    ``default``...).  Serialisation errors are raised before the filesystem is
    touched.
    """
    write_text_atomic(path, json.dumps(data, **dump_kwargs))


def write_text_atomic(path: str, text: str, encoding="utf-8") -> None:
    """Write *text* to *path*, replacing it only once fully written.

    ``encoding=None`` uses the locale's default, as a plain ``open`` does.
    """
    with atomic_output(path, encoding=encoding) as handle:
        handle.write(text)
