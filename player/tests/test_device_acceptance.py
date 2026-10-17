import json
import xml.etree.ElementTree as ET

import pytest

from player.tools.test_device import DeviceTest, ready_for_movement, startup_pid


def report(x, *, cutscene=False, paused=False):
    return {'position': [x, 64, 10], 'input_blocked': cutscene, 'paused': paused}


def test_cutscene_end_and_spawn_pushout_cannot_count_as_input_movement():
    assert not ready_for_movement([report(0, cutscene=True), report(0), report(130)])
    assert not ready_for_movement([report(0), report(130), report(140)])
    assert not ready_for_movement([report(140), report(140, paused=True), report(140)])
    assert not ready_for_movement([report(140), report(140)])
    assert ready_for_movement([report(140), report(140), report(140)])
    drifting = dict(report(140), movement_input=[0, -1])
    assert not ready_for_movement([drifting, drifting, drifting])


class RecordedDevice(DeviceTest):
    """Recorded poses: validate the acceptance gate, not the game simulation."""
    def __init__(self, output, final_x):
        super().__init__('unused-adb', None, output)
        def at(x, timestamp):
            return dict(report(x), _time=timestamp, map='maps/_SHOWCASE.json')
        idle = [at(0, 1), at(0, 2), at(0, 3)]
        forward = idle + [at(100, 4), at(120, 5)]
        self.recordings = iter([idle, forward, forward + [at(final_x, 6)]])

    def reports(self, pid):
        return next(self.recordings)

    def verify_import(self, world):
        return 'recorded acceptance-gate fixture'

    def nodes(self):
        return []

    def screenshot(self, name):
        return 640, 360

    def adb(self, *args, **kwargs):
        return 'recorded test device'


def test_passive_motion_in_one_direction_does_not_pass_touch_acceptance(tmp_path, monkeypatch):
    monkeypatch.setattr('player.tools.test_device.time.sleep', lambda _: None)
    device = RecordedDevice(tmp_path, final_x=140)
    with pytest.raises(RuntimeError, match='movement was not demonstrated'):
        device.validate_world('123', '_SHOWCASE.fiopak')
    assert json.loads((tmp_path / 'result.json').read_text())['passed'] is False


def test_forward_then_reverse_motion_passes_acceptance_gate(tmp_path, monkeypatch):
    monkeypatch.setattr('player.tools.test_device.time.sleep', lambda _: None)
    device = RecordedDevice(tmp_path, final_x=60)
    device.validate_world('123', '_SHOWCASE.fiopak')
    result = json.loads((tmp_path / 'result.json').read_text())
    assert result['passed'] is True
    assert result['forward_position'][0] > result['start_position'][0]
    assert result['end_position'][0] < result['forward_position'][0]


def test_immediate_crash_logs_use_only_the_current_fio_launch_pid():
    events = '''100.0 1 1 I am_proc_start: [0,111,10150,org.vicioussquid.fio.fioplayer,activity]
201.0 1 1 I am_proc_start: [0,222,10151,another.application,activity]
202.0 1 1 I am_proc_start: [0,333,10150,org.vicioussquid.fio.fioplayer,activity]
'''
    assert startup_pid(events, 200) == '333'
    assert startup_pid(events, 203) is None


def test_android_cannot_pass_with_a_different_imported_archive(tmp_path):
    world = tmp_path / '_SHOWCASE.fiopak'
    world.write_bytes(b'desktop export')
    test = DeviceTest('unused-adb', None, tmp_path / 'evidence')
    test.adb = lambda *args: '0' * 64 + ' files/imported.fiopak'
    with pytest.raises(RuntimeError, match='does not match the desktop export'):
        test.verify_import(world)


def test_picker_waits_for_downloads_without_restarting_provider_query(tmp_path, monkeypatch):
    monkeypatch.setattr('player.tools.test_device.time.sleep', lambda _: None)
    breadcrumb = ET.Element('node', text='Downloads', bounds='[0,72][110,120]',
                            attrib={'resource-id': 'com.android.documentsui:id/breadcrumb_text'})
    world = ET.Element('node', text='_SHOWCASE.fiopak', bounds='[20,230][600,300]')
    snapshots = iter([[breadcrumb], [breadcrumb], [breadcrumb, world]])
    test = DeviceTest('unused-adb', None, tmp_path / 'evidence')
    test.nodes = lambda: next(snapshots)
    test.screenshot = lambda _: (640, 360)
    taps = []
    test.adb = lambda *args: taps.append(args)
    test.select_world('_SHOWCASE.fiopak')
    assert taps == [('shell', 'input', 'tap', 310, 265)]
