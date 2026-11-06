"""The cross-platform layout comparison passes only identical editors."""

import copy

from tools.compare_editor_layout import compare, report


def record(platform):
    return {
        "platform": platform, "size": [1600, 900],
        "docks": {"SceneDock": [0, 60, 200, 800]},
        "widgets": [{"key": "QMainWindow/QTabBar[0]", "class": "QTabBar", "text": "",
                     "enabled": True, "rect": [10, 20, 300, 30],
                     "tabs": [{"text": "Top (XZ)", "rect": [0, 0, 150, 30]}],
                     "font": {"family": "Fio UI", "point_size": 11.0, "height": 15,
                              "sample_width": 98}}],
    }


def test_identical_platforms_pass_and_report_the_hierarchy_width():
    ok, lines = report({name: record(name) for name in ("windows", "macos", "linux-x11")},
                       expect=("windows", "macos", "linux-x11"))
    assert ok
    assert "macos: identical" in lines
    assert lines[-1] == "Scene hierarchy width: windows 200, macos 200, linux-x11 200"


def test_a_one_pixel_or_font_difference_fails():
    mac = record("macos")
    mac["widgets"][0]["rect"][2] += 1
    mac["widgets"][0]["font"]["sample_width"] = 99
    found = compare(record("windows"), mac)
    assert len(found) == 2
    assert not report({"windows": record("windows"), "macos": mac})[0]


def test_dock_size_missing_controls_and_platforms_fail():
    mac = record("macos")
    mac["docks"]["SceneDock"][2] = 400
    assert compare(record("windows"), mac) == ["dock SceneDock: [0, 60, 200, 800] != [0, 60, 400, 800]"]
    wayland = copy.deepcopy(record("linux-wayland"))
    wayland["widgets"] = []
    assert "only on windows" in compare(record("windows"), wayland)[0]
    ok, lines = report({"windows": record("windows")}, expect=("windows", "macos"))
    assert not ok and lines[0] == "Missing layout records: macos"


def test_a_different_client_size_is_reported_alone():
    small = record("windows")
    small["size"] = [1024, 707]
    assert compare(record("linux-x11"), small) == ["window client size [1600, 900] != [1024, 707]"]


def test_font_rounding_is_allowed_only_up_to_the_tolerance():
    linux = record("linux-x11")
    linux["widgets"][0]["rect"][2] += 1
    linux["widgets"][0]["font"]["height"] = 16
    rounded = []
    assert compare(record("windows"), linux, tolerance=1, rounded=rounded) == []
    assert len(rounded) == 2
    ok, lines = report({"windows": record("windows"), "linux-x11": linux},
                       expect=("windows", "linux-x11"), tolerance=1)
    assert ok and "linux-x11: identical (2 within 1px font rounding)" in lines

    linux["widgets"][0]["rect"][2] += 1
    assert len(compare(record("windows"), linux, tolerance=1)) == 1


def test_docks_text_and_font_family_have_no_tolerance():
    mac = record("macos")
    mac["docks"]["SceneDock"][2] = 201
    mac["widgets"][0]["text"] = "other"
    mac["widgets"][0]["font"]["family"] = "Helvetica"
    assert len(compare(record("windows"), mac, tolerance=1)) == 3
