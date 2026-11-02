"""No GPU required: honest on/off occlusion performance reporting."""
import importlib.util
from pathlib import Path

import pytest

MODULE = Path(__file__).resolve().parents[2] / "tools" / "occlusion_compare.py"
spec = importlib.util.spec_from_file_location("_fio_occlusion_compare", MODULE)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


def _run(enabled, ready=True):
    return [{
        "map": "test.json",
        "frames": 300,
        "viewport": [1280, 720],
        "brushes": 20000,
        "occlusion_enabled": enabled,
        "occlusion_ready": ready,
        "paint_cpu_ms_median": 2.1 if enabled else 2.4,
        "paint_ms_median": 9.0 if enabled else 9.8,
        "frame_ms_median": 13.0 if enabled else 14.0,
        "frame_ms_p95": 17.0 if enabled else 19.0,
        "occlusion_cpu_overhead_ms_median": .12,
        "occlusion_cpu_index_ms_median": .01,
        "occlusion_probes_issued_mean": 2.2,
        "occlusion_probes_issued_max": 4,
        "occlusion_skipped_mean": 1200,
    }]


def test_performance_report_compares_measured_frame_times():
    x = report.compare(_run(False), _run(True))
    assert len(x) == 1
    assert x[0]["delta_ms_on_minus_off"]["paint_cpu_ms_median"] == -0.3
    assert x[0]["cpu_benefit"]
    assert x[0]["frame_benefit"]


def test_performance_report_does_not_infer_benefit_from_skip_counts():
    on = _run(True)
    on[0]["paint_cpu_ms_median"] = 3.0
    on[0]["frame_ms_median"] = 16.0
    x = report.compare(_run(False), on)
    assert x[0]["skipped_brushes_mean"] == 1200
    assert not x[0]["cpu_benefit"]
    assert not x[0]["frame_benefit"]


def test_performance_report_rejects_unmatched_device_workloads():
    for field, changed in (("frames", 5), ("viewport", [400, 400]),
                           ("brushes", 3)):
        off, on = _run(False), _run(True)
        on[0][field] = changed
        with pytest.raises(ValueError, match=field):
            report.compare(off, on)
    with pytest.raises(ValueError, match="working occlusion"):
        report.compare(_run(False), _run(True, ready=False))
