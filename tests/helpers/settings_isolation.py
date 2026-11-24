"""Editor windows in tests keep their settings in a file of their own.

``MainWindow`` reads and saves ``settings.ini`` next to the project
(``editor.user_paths.settings_path``). Built from the repository root -- as
FioTestSession and the tests that need the real asset folder build it -- that
is the tracked file, and every window, every saved layout or recent-files
entry, and every concurrent test run would share and rewrite it.

:func:`install` replaces ``settings_path`` with one that hands back the
current target (:func:`use`) wherever it would have answered the repository's
own ``settings.ini``; any other root resolves as before (``editor.user_paths``
is tested on its own). It is installed at the source module before the editor
is imported, so the editor's own binding (``from editor.user_paths import
settings_path``) is the redirect too. The target is read at call time, so the
binding never goes stale between tests.

conftest.py gives every test a fresh copy of the tracked defaults;
FioTestSession, used outside pytest, gives itself one for its lifetime.
"""

from __future__ import annotations

import configparser
import os
import sys

_REPO_SETTINGS = os.path.normcase(os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', '..', 'settings.ini')))

_state = {'original': None, 'target': None}


def tracked_settings_path():
    """The repository's own settings.ini (shipped defaults)."""
    return _REPO_SETTINGS


def _redirect(root_dir):
    path = _state['original'](root_dir)
    target = _state['target']
    if target is not None and os.path.normcase(os.path.abspath(path)) == _REPO_SETTINGS:
        return target
    return path


def install():
    """Route settings_path through the redirect (idempotent)."""
    import editor.user_paths as user_paths
    if _state['original'] is None:
        _state['original'] = user_paths.settings_path
        _redirect.__wrapped__ = user_paths.settings_path
    user_paths.settings_path = _redirect
    main_window = sys.modules.get('editor.main_window')
    if main_window is not None:
        main_window.settings_path = _redirect


def use(path):
    """Editor windows read and save *path* in place of the tracked file
    (None: the tracked file, as outside tests)."""
    _state['target'] = None if path is None else str(path)


def redirected():
    """Whether windows are kept off the tracked file right now."""
    return _state['target'] is not None


def fresh_copy(path):
    """Write isolated test settings with the legacy renderer selected.

    The established integration and visual suite exercises the original
    Forward/Classic renderer; tests for HD_High construct it explicitly, so
    their target is never implicit. Whichever renderer the shipped settings
    select, each editor test's is chosen here, and the shipped settings are
    left untouched.
    """
    config = configparser.ConfigParser()
    try:
        config.read(_REPO_SETTINGS, encoding='utf-8')
    except OSError:
        pass
    if config.has_section('Renderer'):
        config.set('Renderer', 'renderer', 'Classic')
    with open(path, 'w', encoding='utf-8') as handle:
        config.write(handle)
    return str(path)
