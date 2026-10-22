"""Where Fio writes when its own folder is read-only.

Fio keeps settings.ini and autosaves next to itself, which works for a source
checkout and an unzipped Windows or Linux build.  A downloaded Fio.app that
macOS has not been told to trust runs from a randomised read-only copy (App
Translocation), and a bundle in a system location may not be writable either;
there every one of those writes failed -- saving Settings raised "Read-only
file system".  When the install folder cannot be written, the same files live
in the per-user data folder instead:

* macOS: ``~/Library/Application Support/Fio``
* Windows: ``%APPDATA%/Fio``
* elsewhere: ``$XDG_DATA_HOME/Fio`` (``~/.local/share/Fio``)

A writable install keeps using its own folder, exactly as before.
"""

import os
import shutil
import sys

APP_NAME = 'Fio'


def user_data_dir():
    """The per-user folder Fio writes to when its own folder is read-only."""
    home = os.path.expanduser('~')
    if sys.platform == 'darwin':
        base = os.path.join(home, 'Library', 'Application Support')
    elif sys.platform == 'win32':
        base = os.environ.get('APPDATA') or home
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.join(home, '.local', 'share')
    return os.path.join(base, APP_NAME)


def _can_write(path):
    """Whether *path*, or the nearest folder that exists above it, is writable."""
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return bool(path) and os.access(path, os.W_OK)


def writable_dir(root_dir, *parts):
    """``root_dir/parts``, or the same path under user_data_dir() if read-only."""
    local = os.path.join(root_dir, *parts)
    if _can_write(local):
        return local
    return os.path.join(user_data_dir(), *parts)


def settings_path(root_dir):
    """The settings.ini to read and save.

    The install's own file while its folder is writable.  Otherwise a copy in
    user_data_dir(), seeded from the install's file the first time so the
    shipped defaults still apply.
    """
    bundled = os.path.join(root_dir, 'settings.ini')
    if _can_write(root_dir):
        return bundled
    user = os.path.join(user_data_dir(), 'settings.ini')
    if not os.path.exists(user):
        try:
            os.makedirs(os.path.dirname(user), exist_ok=True)
            if os.path.exists(bundled):
                shutil.copyfile(bundled, user)
        except OSError:
            return bundled
    return user
