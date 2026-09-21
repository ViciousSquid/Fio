"""Standalone Fio performance benchmark.

This is deliberately independent of pytest.  The development test suite uses
pytest, but Tools > Benchmark must measure Fio without making pytest a runtime
dependency.

The renderer workload uses Fio's real Renderer_F and world representation.
I/O uses the production IOManager, OutputConnection, LogicRelay and registered
input handlers.  CSG uses the production engine.brush_geometry.clip_brush API.

Run directly:
    python plugins/benchmark/fio_benchmark.py
"""
import json
import math
import os
import platform
import statistics