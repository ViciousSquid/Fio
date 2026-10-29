"""Importing CPU-only renderer helpers must not load OpenGL at collection time."""

import subprocess
import sys
from pathlib import Path


def test_light_stream_import_does_not_load_opengl():
    # Use a fresh interpreter: other tests may already have imported the full
    # renderer, masking the package-level dependency that breaks the no-GPU job.
    check = """
import sys
from engine.renderer.core.light_stream import LightStream
from engine.renderer.core import timed_pass
assert LightStream is not None
assert callable(timed_pass)
assert 'OpenGL' not in sys.modules
assert 'engine.renderer.core.base' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", check], capture_output=True, text=True,
        check=False, cwd=Path(__file__).resolve().parents[2],
    )
    assert result.returncode == 0, result.stdout + result.stderr
