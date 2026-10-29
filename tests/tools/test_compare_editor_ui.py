"""The parity report must expose fallback fonts, larger controls and gaps."""

import copy
import json

from tools.compare_editor_ui import build_report, compare_frames


def widget():
    return {"key": "root/QPushButton[0]", "class": "QPushButton", "text": "Apply",
            "rect": [10, 10, 90, 28], "shorter_than_font": False,
            "font": {"family": "Segoe UI", "point_size": 11, "pixel_size": 15,
                     "weight": 50, "italic": False, "height": 18, "sample_width": 100}}


def test_larger_or_clipped_controls_are_prioritized_separately_from_native_fonts():
    windows = {"widgets": [widget()]}
    mac = copy.deepcopy(windows)
    mac["widgets"][0]["font"]["family"] = "Arial"
    mac["widgets"][0]["rect"][3] = 60
    kinds = {d["kind"] for d in compare_frames(windows, mac)}
    assert "font-family" in kinds
    assert "oversized-macos-control-height" in kinds
    mac["widgets"][0]["rect"][3] = 10
    mac["widgets"][0]["shorter_than_font"] = True
    assert "new-text-height-clipping" in {d["kind"] for d in compare_frames(windows, mac)}


def test_missing_controls_are_reported_in_either_direction():
    present, empty = {"widgets": [widget()]}, {"widgets": []}
    assert compare_frames(present, empty)[0]["kind"] == "missing-control"
    assert compare_frames(empty, present)[0]["windows"] is False


def test_anonymous_qt_wrappers_do_not_hide_control_measurements():
    windows = {'widgets': [widget()]}
    mac = copy.deepcopy(windows)
    mac['widgets'][0]['key'] = 'root/QWidget[1]/QPushButton[0]'
    assert compare_frames(windows, mac) == []
    mac['widgets'][0]['font']['pixel_size'] = 11
    assert [d['kind'] for d in compare_frames(windows, mac)] == ['font-pixel_size']


def test_duplicate_controls_with_different_paths_are_not_guessed():
    windows = {'widgets': [widget(), dict(widget(), key='root/QPushButton[1]')]}
    mac = copy.deepcopy(windows)
    for index, value in enumerate(mac['widgets']):
        value['key'] = f'root/QWidget[1]/QPushButton[{index}]'
    assert len(compare_frames(windows, mac)) == 4
    assert all(d['kind'] == 'missing-control' for d in compare_frames(windows, mac))


def test_incomplete_runner_matrix_cannot_be_presented_as_complete(tmp_path):
    captures, output = tmp_path / "captures", tmp_path / "report"
    for platform in ("win32", "darwin"):
        path = captures / platform
        path.mkdir(parents=True)
        data = {"platform": platform, "font_size": 11, "scale_factor": "1",
                "commit": "abc", "qt_platform": platform, "style": "native",
                "logical_dpi": [96, 96], "native_dpr": 1,
                "errors": [], "frames": [{"id": "settings/editor", "image": None,
                                           "size": [600, 500], "widgets": [widget()]}]}
        (path / "manifest.json").write_text(json.dumps(data))
    result = build_report(captures, output)
    assert len(result["pairs"]) == 1
    assert len(result["missing_platforms"]) == 3
    assert (output / "index.html").exists()
    assert (output / "differences.csv").exists()


def test_complete_capture_matrix_still_fails_for_font_regression(tmp_path, monkeypatch):
    from tools.compare_editor_ui import main
    captures, output = tmp_path / 'captures', tmp_path / 'report'
    paths = []
    for platform in ('win32', 'darwin'):
        for size in (11, 16):
            for scale in ('1', '2'):
                folder = captures / f'{platform}-{size}-{scale}'
                folder.mkdir(parents=True)
                control = widget()
                if platform == 'darwin':
                    control['font']['pixel_size'] = 11
                data = dict(platform=platform, font_size=size, scale_factor=scale,
                            commit='abc', qt_platform=platform, style='shared',
                            logical_dpi=[96, 96], native_dpr=1, errors=[], capture_complete=True,
                            frames=[dict(id='settings/editor', image=None, size=[600, 500], widgets=[control])])
                path = folder / 'manifest.json'
                path.write_text(json.dumps(data))
                paths.append(path)
    monkeypatch.setattr('sys.argv', ['compare', '--captures', str(captures), '--output', str(output)])
    assert main() == 1
    for path in paths:
        data = json.loads(path.read_text())
        data['frames'][0]['widgets'][0]['font']['pixel_size'] = 15
        path.write_text(json.dumps(data))
    assert main() == 0
