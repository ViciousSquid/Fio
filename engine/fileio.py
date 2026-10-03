"""Crash-safe writes for Fio's documents (maps, autosaves, saved games).

Opening the destination with ``open(path, "w")`` truncates it before a single
byte of the new document exists, so anything that fails while the document is
being produced — an entity that will not serialise, a full disk — leaves the
user's previous map or save empty.  Here the document is serialised first,
written beside the destination, and moved over it in one ``os.replace``: the
destination always holds either the old document or the complete new one.
"""

import json
import os


def write_json_atomic(path: str, data, **dump_kwargs) -> None:
    """Write *data* as JSON to *path*, replacing it only once fully written.

    ``dump_kwargs`` are passed to :func:`json.dumps` (``indent``,
    ``default``...).  Serialisation errors are raised before the filesystem is
    touched.
    """
    text = json.dumps(data, **dump_kwargs)
    temporary = "%s.tmp" % path
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
