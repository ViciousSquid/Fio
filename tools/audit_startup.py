"""Whole-engine audit: what starting the editor costs, stage by stage.

    xvfb-run -a python tools/audit_startup.py [--json OUT] [--tree PATH]
                                              [--span MODULE:CLASS.METHOD ...]

Follows ``main.py`` up to the first painted frame of an empty editor -- the
imports, the QApplication, ``MainWindow()``, showing it (which creates the GL
context and runs ``QtGameView.initializeGL``) -- and reports the wall time
and resident memory of each stage, with the parts of ``initializeGL`` timed
one by one (the renderer and its shaders, the texture and sprite preloads,
the shadow maps, the logic thread). ``--span`` times one more method.

One process is one cold start of the interpreter, but the file cache is
whatever the host left: run it twice and read the second. Under software GL
the GL parts measure llvmpipe (shader compilation by LLVM above all), not a
GPU's driver: compare runs on one host. ``settings.ini`` is restored.
"""

import argparse
import importlib
import json
import os
import sys
import time

T0 = time.perf_counter()
ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
ap.add_argument('--json')
ap.add_argument('--span', action='append', default=[], metavar='MODULE:CLASS.METHOD')
ap.add_argument('--tree', default=os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
args = ap.parse_args()
tree = os.path.abspath(args.tree)
os.chdir(tree)
sys.path.insert(0, tree)
os.environ.setdefault('PYOPENGL_ERROR_CHECKING', '0')
os.environ.setdefault('QT_QPA_PLATFORM', 'xcb')
sys.setswitchinterval(0.001)
settings = os.path.join(tree, 'settings.ini')
saved = open(settings, 'rb').read()


def rss_mb():
    with open('/proc/self/statm') as f:
        return int(f.read().split()[1]) * os.sysconf('SC_PAGE_SIZE') / 2**20


stages, spans = [], {}
mark = [T0]


def stage(name):
    now = time.perf_counter()
    stages.append({'stage': name, 'ms': round((now - mark[0]) * 1e3, 1),
                   'rss_mb': round(rss_mb(), 1)})
    mark[0] = now


def span(owner, name, label):
    real, bucket = getattr(owner, name), spans.setdefault(label, [])

    def timed(*a, **k):
        started, rss = time.perf_counter(), rss_mb()
        try:
            return real(*a, **k)
        finally:
            bucket.append(((time.perf_counter() - started) * 1e3, rss_mb() - rss))

    setattr(owner, name, timed)


try:
    stage('interpreter to script')
    from PyQt5.QtCore import Qt                               # noqa: E402,F401
    from PyQt5.QtGui import QSurfaceFormat                    # noqa: E402
    from PyQt5.QtWidgets import QApplication                  # noqa: E402
    stage('import PyQt5')
    from editor.main_window import MainWindow                 # noqa: E402
    stage('import editor.main_window (the engine, NumPy, OpenGL, pygame)')
    from engine.asset_archives import prepare_asset_archives  # noqa: E402
    prepare_asset_archives(tree)
    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setDepthBufferSize(24)
    fmt.setStencilBufferSize(8)
    QSurfaceFormat.setDefaultFormat(fmt)
    app = QApplication([])
    from editor.desktop_style import install_editor_style     # noqa: E402
    install_editor_style(app)
    stage('QApplication, the asset archives, the editor style')

    from engine.qt_game_view import QtGameView                # noqa: E402
    import engine.qt_game_view as game_view                   # noqa: E402
    from engine.renderer.forward.lighting import LightingMixin  # noqa: E402
    span(QtGameView, 'initializeGL', 'initializeGL')
    span(game_view, 'create_renderer', '  create_renderer (shaders)')
    span(LightingMixin, '_init_shadow_resources', '    of which the shadow cube maps')
    span(QtGameView, 'load_all_sprite_textures', '  load_all_sprite_textures')
    span(QtGameView, 'preload_level_textures', '  preload_level_textures')
    span(QtGameView, '_start_logic_thread', '  _start_logic_thread')
    span(QtGameView, '_init_debug_resources', '  _init_debug_resources')
    span(QtGameView, 'paintGL', 'paintGL (first frames)')
    for spec in args.span:
        module, _, path = spec.partition(':')
        owner = importlib.import_module(module)
        *owners, method = path.split('.')
        for name in owners:
            owner = getattr(owner, name)
        span(owner, method, path)

    window = MainWindow(tree)
    stage('MainWindow()')
    window.showMaximized()
    view = window.view_3d
    deadline = time.perf_counter() + 30.0
    while not spans['paintGL (first frames)'] and time.perf_counter() < deadline:
        app.processEvents()
    stage('shown, to the first painted 3D frame (initializeGL is in here)')
    total_ms = (time.perf_counter() - T0) * 1e3
    settle = time.perf_counter() + 1.0
    while time.perf_counter() < settle:
        app.processEvents()
        time.sleep(0.005)
    report = {
        'total_ms_to_first_frame': round(total_ms, 1),
        'rss_mb_first_frame': stages[-1]['rss_mb'],
        'rss_mb_settled': round(rss_mb(), 1),
        'stages': stages,
        'inside': {label: {'calls': len(b), 'ms': round(sum(x[0] for x in b), 1),
                           'rss_mb': round(sum(x[1] for x in b), 1)}
                   for label, b in spans.items()},
        'textures_loaded': len(view.renderer.texture_manager),
    }
    text = json.dumps(report, indent=1)
    print(text)
    if args.json:
        with open(args.json, 'w') as f:
            f.write(text)
finally:
    with open(settings, 'wb') as f:
        f.write(saved)
os._exit(0)
