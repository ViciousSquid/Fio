"""Confining map- and model-supplied asset names to the project.

A map, a model's ``mtllib``/``map_Kd`` line and a glTF buffer URI are data
from outside: any of them may name ``/etc/passwd``, ``..\\..\\secret.png``
or ``\\\\host\\share\\x.png`` (which on Windows makes an outbound SMB
connection merely by being probed). Everything that turns such a name into a
file path goes through here first.

The checks are lexical before they touch the file system, and they apply
POSIX *and* Windows rules whatever the host: a name refused on Windows is
refused on Linux too, so a map behaves the same everywhere.

* :func:`safe_relative` -- the name as a clean ``a/b/c`` relative path, or None.
* :func:`confined_path` -- that name under a root folder, or None when it (or
  a symlink it goes through) leaves the root.
* :func:`project_asset_path` -- a project path (``assets/models/x.obj``,
  ``plugins/tidy/assets/book.obj``), or None; an absolute path is accepted
  only when it already lies inside the project.
* :func:`asset_root` -- the folder a model's companions must stay in.

GL-, Qt- and Pillow-free. :mod:`engine.speaker_audio` applies the same rules
to sounds.
"""
from __future__ import annotations

import ntpath
import os
import posixpath

#: Top-level project folders an authored asset path may point into.
PROJECT_ASSET_FOLDERS = ('assets', 'plugins')

_warned: set = set()


def warn_refused(name, what='asset') -> None:
    """One console line per refused name, however often it is asked for."""
    key = (what, str(name))
    if key in _warned:
        return
    if len(_warned) < 4096:                # a hostile map cannot grow it forever
        _warned.add(key)
    print(f"[Assets] Refused {what} path outside the project's assets: {str(name)[:200]!r}")


def _clean(name):
    """*name* as stripped text, or None when it is not a usable relative
    name: not text, empty, NUL, absolute, a drive, UNC or device path, or
    holding any ``:`` (drives, URL schemes, NTFS streams)."""
    if isinstance(name, os.PathLike):
        name = os.fspath(name)
    if not isinstance(name, str):
        return None
    text = name.strip().strip('"').strip()
    if not text or '\0' in text or ':' in text:
        return None
    if (posixpath.isabs(text) or ntpath.isabs(text)
            or ntpath.splitdrive(text)[0] or text[0] in '/\\'):
        return None
    return text


def _normalise(text):
    """``a/./b/../c`` -> ``a/c``; None when ``..`` climbs above the start."""
    parts = []
    for part in text.replace('\\', '/').split('/'):
        if part in ('', '.'):
            continue
        if part == '..':
            if not parts:
                return None
            parts.pop()
        else:
            parts.append(part)
    return '/'.join(parts)


def safe_relative(name):
    """*name* as a normalised ``/``-separated relative path, or None.

    Refused: anything that is not text, empty names, NUL, absolute paths,
    drive letters, UNC and device paths, any ``:`` (drives, URL schemes,
    NTFS streams) and a ``..`` that climbs above where the name starts.
    Both separators count as separators on every OS.
    """
    text = _clean(name)
    return (_normalise(text) or None) if text is not None else None


def is_inside(root, path) -> bool:
    """Whether *path*, with symlinks resolved, is *root* or lies under it."""
    try:
        real_root = os.path.realpath(root)
        return os.path.commonpath([real_root, os.path.realpath(path)]) == real_root
    except (OSError, ValueError):
        return False


def confined_path(root, *names):
    """``root/names...`` as a native path, or None when it would leave *root*.

    *root* is trusted (code chose it); every one of *names* is not. An empty
    name is skipped, so ``confined_path('assets', '', 'x.png')`` works. The
    result need not exist; a symlink leading out of *root* is refused.
    """
    cleaned = []
    for name in names:
        if name is None or (isinstance(name, str) and not name.strip()):
            continue
        text = _clean(name)
        if text is None:
            return None
        cleaned.append(text)
    # '..' may cancel an earlier name, never climb above the root.
    joined = _normalise('/'.join(cleaned)) if cleaned else None
    if not joined:
        return None
    parts = joined.split('/')
    path = os.path.join(os.fspath(root), *parts) if root else os.path.join(*parts)
    if not is_inside(root or os.curdir, path):
        return None
    return path


def _project_roots():
    """Folders that count as the project: the indexed one and the working
    directory (``open('assets/...')`` reads from the latter)."""
    roots = [os.path.abspath(os.getcwd())]
    try:
        from engine.asset_archives import _state
        root = _state.get('root')
        if root is not None and os.path.abspath(os.fspath(root)) not in roots:
            roots.append(os.path.abspath(os.fspath(root)))
    except Exception:
        pass
    return roots


def project_asset_path(path, folders=PROJECT_ASSET_FOLDERS):
    """An authored project path made safe, or None.

    ``assets/models/x.obj`` and ``plugins/tidy/assets/book.obj`` come back
    as native relative paths. An absolute path is accepted only when it is
    lexically inside the project (Fio's own class attributes are written
    that way) and comes back absolute. Either way the first folder must be
    one of *folders* and no symlink may lead out of it.
    """
    if isinstance(path, os.PathLike):
        path = os.fspath(path)
    if not isinstance(path, str) or not path.strip() or '\0' in path:
        return None
    text = path.strip()
    if os.path.isabs(text):
        if text[:2] in ('//', '\\\\', '/\\', '\\/'):
            return None                     # UNC / device: never probed
        absolute = os.path.abspath(text)    # lexical only: no file access
        for root in _project_roots():
            try:
                if os.path.commonpath([root, absolute]) != root:
                    continue
            except ValueError:
                continue
            rel = safe_relative(os.path.relpath(absolute, root))
            if rel is None or rel.split('/')[0] not in folders:
                continue
            native = os.path.join(root, *rel.split('/'))
            if is_inside(os.path.join(root, rel.split('/')[0]), native):
                return native
        return None
    rel = safe_relative(text)
    if rel is None or rel.split('/')[0] not in folders:
        return None
    native = os.path.join(*rel.split('/'))
    if not is_inside(rel.split('/')[0], native):
        return None
    return native


def asset_root(path) -> str:
    """The folder the companions of the model at *path* must stay inside.

    The nearest ``assets`` folder above it (``assets``,
    ``plugins/<name>/assets``, a package's ``assets``), so
    ``../textures/x.png`` beside ``assets/models`` keeps working; for a file
    anywhere else, its own folder.
    """
    folder = os.path.dirname(os.fspath(path))
    probe = folder
    while probe:
        head, tail = os.path.split(probe)
        if tail == 'assets':
            return probe
        if head == probe:
            break
        probe = head
    return folder


def companion_path(owner, name, base=None):
    """The file *name* names relative to *base* (default: *owner*'s folder),
    confined to :func:`asset_root` of *owner*; None when refused."""
    root = asset_root(owner)
    folder = os.path.dirname(os.fspath(owner)) if base is None else os.fspath(base)
    rel = _relative_to(root, folder)
    if rel is None:
        return None
    return confined_path(root, rel, name) if rel else confined_path(root, name)


def _relative_to(root, folder):
    """*folder* relative to *root* (``''`` when equal), None when outside."""
    a = os.path.normpath(root) if root else ''
    b = os.path.normpath(folder) if folder else ''
    if a in ('', '.'):
        if b in ('', '.'):
            return ''
        return None if os.path.isabs(b) or b.split(os.sep)[0] == '..' else b
    if b == a:
        return ''
    prefix = a.rstrip(os.sep) + os.sep
    return b[len(prefix):] if b.startswith(prefix) else None
