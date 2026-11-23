"""Visual tier: Classic draws what it always drew, pixel for pixel.

The stored frames and fragment sources under ``tests/visual/golden/`` were
drawn before any HD renderer code existed (tools/hd_golden.py --update, at the
commit that added this test). The Classic renderer -- Fio's default -- must
still draw exactly them: the stock maps from fixed cameras in the editor and
in Play, and every program's final fragment source as the driver holds it.

The capture runs in a process of its own (it needs ``PYTHONHASHSEED=0`` and
the working directory of ``main.py``). Frames are compared on the driver that
stored them; elsewhere only the shader sources are, and
``tools/hd_golden.py --base`` compares two checkouts instead.
"""

import os
import subprocess
import sys

import pytest

from tests.helpers.paths import REPO_ROOT

pytestmark = [pytest.mark.gl, pytest.mark.slow]


def test_classic_draws_the_stored_frames_from_the_stored_shaders():
    environment = dict(os.environ, PYTHONHASHSEED="0")
    environment.pop("QT_QPA_PLATFORM", None)
    # The capture is its own process with its own look: nothing an earlier
    # test set for Qt (a scale factor changes the view's size in pixels).
    for name in list(environment):
        if name.startswith("QT_") and name != "QT_QPA_PLATFORM_PLUGIN_PATH":
            del environment[name]
    result = subprocess.run(
        [sys.executable, os.path.join(REPO_ROOT, "tools", "hd_golden.py"), "--check"],
        cwd=REPO_ROOT, env=environment, capture_output=True, text=True, timeout=1500)
    report = [line for line in result.stdout.splitlines()
              if line.startswith(("DIFFERENT", "identical", "frames not compared"))
              or "differences" in line]
    assert result.returncode == 0, "\n".join(report) or result.stdout[-3000:] + result.stderr[-3000:]
    assert "identical" in report, result.stdout[-2000:]
