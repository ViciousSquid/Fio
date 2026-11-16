"""tools/run_qt_suite_shard.py: one module cannot hold a whole shard hostage.

A Windows ARM64 shard once hung in a single test until the 90-min job limit,
and the 25 modules after it never ran. The runner now stops a module's
process after MODULE_TIMEOUT_SECONDS, counts it as failed and carries on.

A module that shows real message boxes (marked native_message_box) runs on
the windows platform where offscreen cannot show one: Windows with Qt 5.15.2.
"""

import sys
import time

from tools.run_qt_suite_shard import (MODULE_TIMEOUT_SECONDS, TEST_STACK_DUMP_SECONDS,
                                      collect_modules, message_boxes_need_native_platform,
                                      partition_modules, run_module)


def test_a_module_that_finishes_reports_its_exit_code(tmp_path):
    assert run_module([sys.executable, "-c", "raise SystemExit(3)"], tmp_path) == (3, False)
    assert run_module([sys.executable, "-c", "pass"], tmp_path) == (0, False)


def test_a_module_that_hangs_is_stopped_and_reported(tmp_path):
    started = time.monotonic()
    result = run_module([sys.executable, "-c", "import time; time.sleep(60)"],
                        tmp_path, timeout=1)
    assert result == (None, True)
    assert time.monotonic() - started < 30


def test_stacks_are_dumped_well_before_a_module_is_stopped():
    assert 0 < TEST_STACK_DUMP_SECONDS < MODULE_TIMEOUT_SECONDS / 2


def test_partition_keeps_every_module_exactly_once():
    counts = {"m%02d.py" % i: (i * 7) % 23 + 1 for i in range(40)}
    shards = partition_modules(counts, 6)
    placed = [module for shard in shards for module, _ in shard]
    assert sorted(placed) == sorted(counts)


def test_message_boxes_leave_offscreen_only_where_qt_5_15_2_cannot_show_them():
    need = message_boxes_need_native_platform
    # Windows x64 (PyQt5-Qt5 5.15.2): QMessageBox's show dereferences the
    # offscreen platform's missing native interface.
    assert need("win32", "offscreen", "5.15.2")
    assert need("win32", "offscreen:fontengine=freetype", "5.15.2")
    # Qt 5.15.10 gave the offscreen platform a native interface (ARM64's
    # wheel, Linux and macOS have a newer Qt); on a real platform there is one.
    assert not need("win32", "offscreen", "5.15.10")
    assert not need("win32", "offscreen", "5.15.16")
    assert not need("win32", "windows", "5.15.2")
    assert not need("linux", "offscreen", "5.15.2")
    assert not need("darwin", "offscreen", "5.15.2")


def test_a_module_can_be_run_with_its_own_environment(tmp_path):
    import os
    probe = [sys.executable, "-c",
             "import os; raise SystemExit(0 if os.environ.get('QT_QPA_PLATFORM') "
             "== 'windows' else 5)"]
    assert run_module(probe, tmp_path, env=dict(os.environ, QT_QPA_PLATFORM="windows")) \
        == (0, False)
    assert run_module(probe, tmp_path, env=dict(os.environ, QT_QPA_PLATFORM="offscreen")) \
        == (5, False)


def test_modules_that_show_message_boxes_are_found_by_their_marker(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\nmarkers =\n    qt: q\n    native_message_box: m\n")
    (tmp_path / "test_boxes.py").write_text(
        "import pytest\npytestmark = [pytest.mark.qt, pytest.mark.native_message_box]\n"
        "def test_a(): pass\ndef test_b(): pass\n")
    (tmp_path / "test_plain.py").write_text(
        "import pytest\npytestmark = pytest.mark.qt\ndef test_c(): pass\n")
    monkeypatch.chdir(tmp_path)
    native = set()
    counts = collect_modules(tmp_path, "qt", native)
    assert counts == {"test_boxes.py": 2, "test_plain.py": 1}
    assert native == {"test_boxes.py"}


def test_the_desktop_style_module_is_marked():
    import ast
    from tests.helpers.paths import repo_path
    tree = ast.parse(open(repo_path("tests", "editor", "test_desktop_style.py")).read())
    marks = [node.attr for node in ast.walk(tree)
             if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Attribute)
             and node.value.attr == "mark"]
    assert "native_message_box" in marks
