"""Compare kiosk presentation on the 3D view's widget with the native kiosk window.

    python tools/bench_kiosk_presentation.py [--map maps/_SHOWCASE.json]
        [--seconds 20] [--warmup 4] [--repeats 2] [--mode Fullscreen]
        [--size 1280x720] [--vsync on|off] [--json OUT]

Each run is a separate Fio process: main.py's OpenGL format (3.3 core, 24-bit
depth, 8-bit stencil, the chosen swap interval), the editor window, the map,
kiosk mode in the chosen window mode -- presented by the widget (Qt
composites its framebuffer into the window) or, with ``[Kiosk]
native_surface``, by the native window -- then *warmup* seconds, then
*seconds* of the player walking a fixed route (the same keys at the same
times). Both paths record the same ``PresentationStats``
(engine/kiosk_surface.py) per presented frame: the interval, render time
(paintGL CPU), presentation time (widget: Qt's composition and swap; native:
swapBuffers), which publication it showed (duplicates; publications never
shown) and prepare-to-presented latency. Runs alternate widget, native,
widget, ... so drift (thermal, background load) falls on both; the summary
gives each metric's median over the runs and its range.

A short swap does not mean the frame reached the display sooner (a driver
may queue it and block later); judge by the intervals, duplicates, skipped
publications and latency, which include that wait.

This is the decisive comparison on the target machine (the Snapdragon ARM
build): run it there, on the display the game is played on, with nothing
else running. Each run keeps its settings in a temporary copy; the tracked
settings.ini is never written.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import time

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))


def _child(args):
    sys.path.insert(0, ROOT)
    os.chdir(ROOT)
    # The run keeps its settings in a copy of its own: the tracked
    # settings.ini is never written (several runs may overlap).
    import shutil
    import tempfile
    import editor.user_paths as user_paths
    own_settings = os.path.join(tempfile.mkdtemp(prefix='fio-bench-'), 'settings.ini')
    tracked = os.path.normcase(os.path.abspath(os.path.join(ROOT, 'settings.ini')))
    if os.path.exists(tracked):
        shutil.copyfile(tracked, own_settings)
    original_settings_path = user_paths.settings_path

    def settings_path(root_dir):
        path = original_settings_path(root_dir)
        return own_settings if os.path.normcase(os.path.abspath(path)) == tracked else path
    user_paths.settings_path = settings_path
    from PyQt5.QtCore import Qt, QTimer
    from PyQt5.QtWidgets import QApplication
    from engine.gl_format import set_default_surface_format
    set_default_surface_format(1 if args.vsync == 'on' else 0)
    app = QApplication(sys.argv[:1])
    from editor.desktop_style import install_editor_style
    install_editor_style(app)
    from editor.main_window import MainWindow
    window = MainWindow(ROOT)
    config = window.config
    if not config.has_section('Kiosk'):
        config.add_section('Kiosk')
    width, height = (int(v) for v in args.size.lower().split('x'))
    config.set('Kiosk', 'window_mode', args.mode)
    config.set('Kiosk', 'res_width', str(width))
    config.set('Kiosk', 'res_height', str(height))
    config.set('Kiosk', 'native_surface', 'True' if args.child == 'native' else 'False')
    config.set('Display', 'show_fps', 'False')
    window.show()
    view = window.view_3d
    state = {'phase': 'load'}
    result = {}

    def finish(error=None):
        stats = view.presentation_stats.summary(args.child) if error is None else None
        info = {}
        try:
            from OpenGL import GL
            view.makeCurrent()
            info = {'gl_vendor': GL.glGetString(GL.GL_VENDOR).decode(errors='replace'),
                    'gl_renderer': GL.glGetString(GL.GL_RENDERER).decode(errors='replace'),
                    'gl_version': GL.glGetString(GL.GL_VERSION).decode(errors='replace')}
            view.doneCurrent()
        except Exception:
            pass
        result.update(path=args.child, native_active=view._native_surface is not None,
                      stats=stats, error=error, platform=QApplication.platformName(),
                      **info)
        print('BENCH_RESULT ' + json.dumps(result), flush=True)
        window.unsaved_changes = False
        os._exit(0)

    def start():
        try:
            if not window.load_level_file(os.path.join(ROOT, args.map)):
                finish('the map did not open')
                return
            window.enter_kiosk_mode()
        except Exception as exc:                       # reported, not hung
            finish('%s: %s' % (type(exc).__name__, exc))
            return
        if args.child == 'native' and view._native_surface is None:
            finish('native presentation was refused (see the Debug Console log)')
            return
        state.update(phase='warm', t0=time.perf_counter())

    def drive():
        if state['phase'] == 'load':
            return
        t = time.perf_counter() - state['t0']
        # The route: walk forward, turning left one second in six.
        window.keys_pressed = {Qt.Key_W} if int(t) % 6 < 5 else {Qt.Key_W, Qt.Key_A}
        if state['phase'] == 'warm' and t >= args.warmup:
            view.presentation_stats.clear()
            state.update(phase='measure', t1=time.perf_counter())
        elif state['phase'] == 'measure' and time.perf_counter() - state['t1'] >= args.seconds:
            state['phase'] = 'done'
            finish()

    QTimer.singleShot(500, start)
    timer = QTimer()
    timer.timeout.connect(drive)
    timer.start(50)
    app.exec_()


def _run(args, path):
    command = [sys.executable, os.path.abspath(__file__), '--child', path,
               '--map', args.map, '--seconds', str(args.seconds), '--warmup', str(args.warmup),
               '--mode', args.mode, '--size', args.size, '--vsync', args.vsync]
    timeout = args.seconds + args.warmup + 180
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return {'path': path, 'error': 'timed out'}
    for line in out.splitlines():
        if line.startswith('BENCH_RESULT '):
            return json.loads(line[len('BENCH_RESULT '):])
    return {'path': path, 'error': 'no result (the run crashed?)\n' + out[-2000:]}


#: What each run reports, and how the summary prints it.
METRICS = (
    ('fps', 'fps', '%.1f'),
    ('interval_ms_p50', 'interval p50 ms', '%.2f'),
    ('interval_ms_p95', 'interval p95 ms', '%.2f'),
    ('interval_ms_p99', 'interval p99 ms', '%.2f'),
    ('doubled_frames', 'long intervals (>1.5x median)', '%d'),
    ('duplicate_frames', 'duplicate frames (same publication)', '%d'),
    ('skipped_publications', 'publications never shown', '%d'),
    ('paint_ms_p50', 'render (paintGL CPU) p50 ms', '%.2f'),
    ('present_ms_p50', 'present p50 ms', '%.2f'),
    ('present_ms_p95', 'present p95 ms', '%.2f'),
    ('latency_ms_p50', 'prepare-to-presented p50 ms', '%.2f'),
    ('latency_ms_p95', 'prepare-to-presented p95 ms', '%.2f'),
)


def _row(label, stats):
    if not stats:
        return '%-8s  (no frames)' % label
    return ('%-8s %6.1f fps  interval p50 %6.2f  p99 %6.2f ms  long %3d  dup %3d  skipped %3d  '
            'render %6.2f ms  present p50 %6.2f ms  latency p50 %6.2f ms' % (
                label, stats['fps'] or 0.0, stats['interval_ms_p50'] or 0.0,
                stats['interval_ms_p99'] or 0.0, stats['doubled_frames'],
                stats['duplicate_frames'], stats['skipped_publications'],
                stats['paint_ms_p50'], stats['present_ms_p50'],
                stats['latency_ms_p50'] or 0.0))


def _summary(runs):
    """Per metric and path: the median over the runs, and their range --
    the spread says how much one run can be trusted."""
    import statistics
    lines = ['\n%-38s %-28s %-28s' % ('median over runs [min - max]', 'widget', 'native')]
    for key, label, fmt in METRICS:
        cells = []
        for path in ('widget', 'native'):
            values = [r['stats'][key] for r in runs
                      if r.get('path') == path and r.get('stats') and r['stats'].get(key) is not None]
            if not values:
                cells.append('-')
                continue
            cells.append((fmt + ' [' + fmt + ' - ' + fmt + ']') % (
                statistics.median(values), min(values), max(values)))
        lines.append('%-38s %-28s %-28s' % (label, cells[0], cells[1]))
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--map', default='maps/_SHOWCASE.json')
    parser.add_argument('--seconds', type=float, default=20.0)
    parser.add_argument('--warmup', type=float, default=4.0)
    parser.add_argument('--repeats', type=int, default=2)
    parser.add_argument('--mode', default='Fullscreen', choices=('Fullscreen', 'Borderless', 'Windowed'))
    parser.add_argument('--size', default='1280x720', help='windowed size, WxH')
    parser.add_argument('--vsync', default='on', choices=('on', 'off'))
    parser.add_argument('--json')
    parser.add_argument('--child', choices=('widget', 'native'), help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.child:
        _child(args)
        return
    runs = []
    for _ in range(args.repeats):
        for path in ('widget', 'native'):
            result = _run(args, path)
            runs.append(result)
            print(_row(path, result.get('stats')) if not result.get('error')
                  else '%-8s  ERROR: %s' % (path, result['error']), flush=True)
    print(_summary(runs))
    first = next((r for r in runs if r.get('gl_renderer')), {})
    print('\n%s, %s; Qt platform %s; GL: %s / %s / %s; map %s; %s %s; vsync %s' % (
        platform.system(), platform.machine(), first.get('platform', '?'),
        first.get('gl_vendor', '?'), first.get('gl_renderer', '?'), first.get('gl_version', '?'),
        args.map, args.mode, args.size if args.mode == 'Windowed' else '', args.vsync))
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as handle:
            json.dump({'args': vars(args), 'machine': platform.machine(),
                       'system': platform.system(), 'runs': runs}, handle, indent=1)


if __name__ == '__main__':
    main()
