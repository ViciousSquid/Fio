"""Install a debug APK and test an exported world through Android's picker.

Requires Python 3 and Android SDK platform-tools on your computer. The phone
must be unlocked, USB debugging authorized, and connected. Logs are limited to
the Fio process. Screenshots capture the Fio app and Android's file picker.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path
import re
import struct
import subprocess
import time
import xml.etree.ElementTree as ET

PACKAGE = 'org.vicioussquid.fio.fioplayer'
ACTIVITY = PACKAGE + '/org.kivy.android.PythonActivity'


def bounds_center(bounds):
    values = [int(v) for v in re.findall(r'\d+', bounds)]
    if len(values) != 4:
        raise ValueError(f'Invalid UI bounds: {bounds}')
    x1, y1, x2, y2 = values
    return (x1 + x2) // 2, (y1 + y2) // 2


def ready_for_movement(reports):
    """Exclude cutscene motion and let spawn collision resolution settle."""
    recent = reports[-3:]
    if len(recent) != 3 or any(r['paused'] or r.get('input_blocked') for r in recent):
        return False
    if any(any(abs(axis) > .05 for axis in r.get('movement_input', (0, 0))) for r in recent):
        return False
    start = recent[0]['position']
    return all(math.hypot(r['position'][0] - start[0], r['position'][2] - start[2]) < 1
               for r in recent[1:])


def startup_pid(events, since):
    """Recover Fio's PID even when it exits before the first pidof query."""
    pattern = re.compile(r'\[\d+,(\d+),\d+,' + re.escape(PACKAGE) + r',')
    found = None
    for line in events.splitlines():
        match = pattern.search(line)
        if match:
            try:
                if float(line.split()[0]) >= since:
                    found = match.group(1)
            except (ValueError, IndexError):
                pass
    return found


class DeviceTest:
    def __init__(self, adb, serial, output, timeout_scale=1):
        self.command = [adb] + (['-s', serial] if serial else [])
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.timeout_scale = max(1., min(10., timeout_scale))
        self.pid = None
        (self.output / 'result.json').write_text(json.dumps({'passed': False, 'status': 'running'}), encoding='utf-8')

    def adb(self, *args, binary=False, check=True):
        result = subprocess.run([*self.command, *map(str, args)],
                                capture_output=True, timeout=90 * self.timeout_scale)
        if check and result.returncode:
            detail = (result.stderr + result.stdout).decode('utf-8', 'replace').strip()
            raise RuntimeError(f'ADB {args[0]} failed ({result.returncode}): {detail[:2000]}')
        return result.stdout if binary else result.stdout.decode('utf-8', 'replace')

    def screenshot(self, name):
        raw = self.adb('exec-out', 'screencap', '-p', binary=True)
        if raw[:8] != b'\x89PNG\r\n\x1a\n':
            raise RuntimeError('Android did not return a PNG screenshot')
        (self.output / name).write_bytes(raw)
        return struct.unpack_from('>II', raw, 16)

    def nodes(self):
        # A failed dump must never reuse a previous window's coordinates.
        self.adb('shell', 'rm', '-f', '/data/local/tmp/fio-window.xml', check=False)
        self.adb('shell', 'uiautomator', 'dump', '/data/local/tmp/fio-window.xml', check=False)
        raw = self.adb('shell', 'cat', '/data/local/tmp/fio-window.xml', check=False)
        try:
            return list(ET.fromstring(raw).iter('node'))
        except ET.ParseError:
            return []

    def select_world(self, name):
        deadline = time.monotonic() + 60 * self.timeout_scale
        opened_roots = False
        while time.monotonic() < deadline:
            nodes = self.nodes()
            if self.dismiss_fullscreen_hint(nodes):
                continue
            # Prefer the exact filename; some providers hide its extension.
            for node in nodes:
                if node.get('text', '') in (name, Path(name).stem):
                    self.screenshot('package-picker.png')
                    self.adb('shell', 'input', 'tap', *bounds_center(node.get('bounds', '')))
                    return
            # Large grid cards can put a filename below a short landscape
            # viewport. The system picker's list view keeps it accessible.
            list_toggle = next((n for n in nodes if
                                n.get('content-desc') in ('List view', 'Show list', 'Show as list')
                                or n.get('resource-id', '').rsplit('/', 1)[-1] in ('menu_list', 'option_menu_list')), None)
            if list_toggle is not None:
                self.adb('shell', 'input', 'tap', *bounds_center(list_toggle.get('bounds', '')))
                continue
            # Do not click the Downloads breadcrumb while its provider is
            # loading: that restarts the query and can starve a slow emulator.
            if any((n.get('text') in ('Download', 'Downloads') and n.get('resource-id', '').endswith('/breadcrumb_text'))
                   or n.get('text') == 'Files in Downloads' for n in nodes):
                time.sleep(2)
                continue
            for node in nodes:
                if node.get('text') == 'Downloads':
                    self.adb('shell', 'input', 'tap', *bounds_center(node.get('bounds', '')))
                    break
                if not opened_roots and node.get('content-desc') in ('Show roots', 'Show navigation drawer'):
                    self.adb('shell', 'input', 'tap', *bounds_center(node.get('bounds', '')))
                    opened_roots = True
                    break
            time.sleep(1)
        self.screenshot('picker-failed.png')
        raise RuntimeError('Could not find the world in the Android file picker; see picker-failed.png')

    def dismiss_fullscreen_hint(self, nodes):
        # Android can show this first-use overlay again when returning from
        # DocumentsUI. It intercepts touch input while the world keeps running.
        for node in nodes:
            if node.get('package') == 'com.android.systemui' and node.get('text') == 'Got it':
                self.adb('shell', 'input', 'tap', *bounds_center(node.get('bounds', '')))
                print('Dismissed Android fullscreen introduction.', flush=True)
                return True
        return False

    def reports(self, pid):
        log = self.adb('logcat', '-d', '--pid', pid, '-v', 'epoch')
        (self.output / 'fio-logcat.txt').write_text(log, encoding='utf-8')
        result = []
        for line in log.splitlines():
            marker = '[FIO_VALIDATION] '
            if marker in line:
                try:
                    report = json.loads(line.split(marker, 1)[1])
                    report['_time'] = float(line.split()[0])
                    result.append(report)
                except (json.JSONDecodeError, ValueError, IndexError):
                    pass
        return result

    def wait_for_new_report(self, pid, since):
        """A slow emulator may render fewer than one report per gesture."""
        deadline = time.monotonic() + 30 * self.timeout_scale
        while time.monotonic() < deadline:
            reports = self.reports(pid)
            if (reports and reports[-1]['_time'] > since
                    and not any(abs(v) > .05 for v in reports[-1].get('movement_input', (0, 0)))):
                return reports
            if not self.adb('shell', 'pidof', PACKAGE, check=False).strip():
                raise RuntimeError('Fio exited while waiting for movement evidence')
            time.sleep(2)
        raise RuntimeError('Fio stopped producing world reports during movement')

    def run(self, apk, world, *, skip_install=False):
        devices = self.adb('get-state').strip()
        if devices != 'device':
            raise RuntimeError('Connect and authorize one unlocked Android device, or provide --serial')
        deadline = time.monotonic() + 90 * self.timeout_scale
        while 'found' not in self.adb('shell', 'service', 'check', 'package', check=False):
            if time.monotonic() >= deadline:
                raise RuntimeError('Android package service did not become ready')
            time.sleep(2)
        while self.adb('shell', 'getprop', 'sys.boot_completed', check=False).strip() != '1':
            if time.monotonic() >= deadline:
                raise RuntimeError('Android did not finish booting before APK installation')
            time.sleep(2)
        if not skip_install:
            print('Installing debug APK. Existing Fio private files are preserved; a signing-key conflict fails installation.', flush=True)
            # Stop an already rendering Fio first; it otherwise competes with
            # package installation on CPU-only emulators.
            self.adb('shell', 'am', 'force-stop', PACKAGE)
            self.adb('install', '--no-streaming', '-r', str(Path(apk).resolve()))
            print('Debug APK installed.', flush=True)
        self.adb('push', str(Path(world).resolve()), '/sdcard/Download/' + Path(world).name)
        print('Exported world transferred to Android Downloads.', flush=True)
        self.adb('shell', 'am', 'force-stop', PACKAGE)
        launched_at = float(self.adb('shell', 'date', '+%s').strip())
        self.adb('shell', 'am', 'start', '-n', ACTIVITY, '--ez', 'fio_validation', 'true')
        deadline = time.monotonic() + 45 * self.timeout_scale
        pid = ''
        while time.monotonic() < deadline:
            pid = self.adb('shell', 'pidof', PACKAGE, check=False).strip()
            if pid:
                break
            time.sleep(1)
        if not pid:
            events = self.adb('logcat', '-b', 'events', '-d', '-s', 'am_proc_start', '-v', 'epoch', check=False)
            self.pid = startup_pid(events, launched_at)
            raise RuntimeError('Fio did not start; check the phone for an error-report dialog')
        self.pid = pid
        print('Fio process started; waiting for SDL and the renderer.', flush=True)
        # Starting UiAutomator's Java VM while a software emulator is still
        # importing Python competes with startup and can itself time out.
        # A PID alone does not mean the application is ready for interaction.
        deadline = time.monotonic() + 90 * self.timeout_scale
        while time.monotonic() < deadline:
            self.reports(pid)
            log = (self.output / 'fio-logcat.txt').read_text(encoding='utf-8')
            if '[AndroidHost] renderer ready' in log:
                break
            if not self.adb('shell', 'pidof', PACKAGE, check=False).strip():
                raise RuntimeError('Fio exited during startup; inspect fio-logcat.txt')
            time.sleep(3)
        else:
            self.screenshot('startup-failed.png')
            raise RuntimeError('SDL/renderer startup did not complete; inspect fio-logcat.txt / startup-failed.png')
        print('Renderer ready; opening the world through Android\'s picker.', flush=True)
        self.select_world(Path(world).name)
        self.validate_world(pid, world)

    def validate_world(self, pid, world, *, picker_selection='automated'):
        """Check a loaded world; also usable after a manually assisted picker."""
        self.pid = pid
        print('World selected; waiting for rendering and any authored opening cutscene.', flush=True)
        deadline = time.monotonic() + 120 * self.timeout_scale
        before = []
        while time.monotonic() < deadline:
            before = self.reports(pid)
            if ready_for_movement(before):
                break
            time.sleep(2)
        if not ready_for_movement(before):
            self.screenshot('load-failed.png')
            raise RuntimeError('No rendered-world reports: use a debug APK and inspect fio-logcat.txt / load-failed.png')
        world_sha256 = self.verify_import(world)
        self.dismiss_fullscreen_hint(self.nodes())
        width, height = self.screenshot('before-movement.png')
        # Drag from the floating stick's origin, then hold the forward position.
        # Slow software emulators need a longer real hold to deliver several
        # rendered frames; no engine simulation is mocked or advanced here.
        x, y = int(width * .17), int(height * .76)
        hold_ms = int(3000 * self.timeout_scale)
        for _ in range(2):
            self.adb('shell', 'input', 'touchscreen', 'swipe', x, y, x, int(height * .53), hold_ms)
        forward_ended = float(self.adb('shell', 'date', '+%s').strip())
        forward = self.wait_for_new_report(pid, forward_ended)
        self.screenshot('after-forward.png')
        forward_reports = [r for r in forward if r['_time'] > before[-1]['_time']]
        if not forward_reports:
            raise RuntimeError('Fio stopped producing world reports during movement')
        start = before[-1]['position']
        distances = [math.dist(start, report['position']) for report in forward_reports]
        horizontal = [math.hypot(report['position'][0] - start[0], report['position'][2] - start[2])
                      for report in forward_reports]
        # Opposite input must reverse the direction. This rules out passive
        # platform motion or a late scripted push masquerading as touch input.
        for _ in range(2):
            self.adb('shell', 'input', 'touchscreen', 'swipe', x, y, x, int(height * .95), hold_ms)
        reverse_ended = float(self.adb('shell', 'date', '+%s').strip())
        after = self.wait_for_new_report(pid, reverse_ended)
        self.screenshot('after-movement.png')
        if not after or after[-1]['_time'] <= forward[-1]['_time']:
            raise RuntimeError('Fio stopped producing world reports during reverse movement')
        middle, end = forward[-1]['position'], after[-1]['position']
        dx, dz = middle[0] - start[0], middle[2] - start[2]
        reverse_progress = -((end[0] - middle[0]) * dx + (end[2] - middle[2]) * dz) / max(math.hypot(dx, dz), .001)
        result = {'package': PACKAGE, 'world': Path(world).name, 'map': after[-1]['map'],
                  'world_sha256': world_sha256, 'android_import_sha256': world_sha256,
                  'picker_selection': picker_selection,
                  'device_model': self.adb('shell', 'getprop', 'ro.product.model').strip(),
                  'android_release': self.adb('shell', 'getprop', 'ro.build.version.release').strip(),
                  'android_api': self.adb('shell', 'getprop', 'ro.build.version.sdk').strip(),
                  'start_position': start, 'forward_position': middle, 'end_position': end,
                  'max_distance': max(distances), 'max_horizontal_distance': max(horizontal),
                  'reverse_progress': reverse_progress,
                  'passed': max(horizontal) > 16 and reverse_progress > 8 and not after[-1]['paused']}
        (self.output / 'result.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        if not result['passed']:
            raise RuntimeError('The world rendered but movement was not demonstrated; inspect screenshots and result.json')
        print(json.dumps(result, indent=2))
        print(f'PASS: real Android APK loaded the package and moved. Evidence: {self.output.resolve()}')

    def verify_import(self, world):
        expected = hashlib.sha256(Path(world).read_bytes()).hexdigest()
        actual = self.adb('shell', 'run-as', PACKAGE, 'sha256sum', 'files/imported.fiopak').split()[0]
        if actual != expected:
            raise RuntimeError('Imported Android world does not match the desktop export')
        return expected


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('apk', help='Fio debug APK')
    parser.add_argument('world', help='Desktop-exported _SHOWCASE.fiopak')
    parser.add_argument('--adb', default='adb')
    parser.add_argument('--serial')
    parser.add_argument('--output', default='fio-device-results')
    parser.add_argument('--timeout-scale', type=float, default=1, help='Increase waits for a software-only cloud emulator')
    parser.add_argument('--skip-install', action='store_true', help='Retest an already installed debug APK')
    args = parser.parse_args(argv)
    test = DeviceTest(args.adb, args.serial, args.output, args.timeout_scale)
    try:
        test.run(args.apk, args.world, skip_install=args.skip_install)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        result_path = test.output / 'result.json'
        result = json.loads(result_path.read_text(encoding='utf-8'))
        result.update(passed=False, status='failed', error=str(exc))
        result_path.write_text(json.dumps(result, indent=2), encoding='utf-8')
        if test.pid:
            try:
                test.reports(test.pid)
            except (OSError, subprocess.SubprocessError):
                pass
        parser.exit(1, f'Device validation failed: {exc}\n')


if __name__ == '__main__':
    main()
