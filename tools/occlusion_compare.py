"""Compare same-map SQ3 occlusion benchmarks without claiming a synthetic win.

Run `bench_compare.py --occlusion off --json off.json` and then
`bench_compare.py --occlusion on --json on.json` using the same map, viewport
and frame count. This report compares actual rendered-frame CPU and wall-clock
times. It does NOT infer savings from skipped brushes alone.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def compare(off, on):
    """Return signed differences: negative milliseconds mean culling helped."""
    results = []
    before = {str(x["map"]): x for x in off}
    for after in on:
        name = str(after["map"])
        if name not in before:
            raise ValueError(f"Map missing from off run: {name}")
        baseline = before[name]
        for field in ("frames", "viewport", "brushes"):
            if baseline[field] != after[field]:
                raise ValueError(f"{name}: {field} differs; A/B comparison invalid")
        if baseline.get("occlusion_enabled") is not False:
            raise ValueError(f"{name}: baseline must use --occlusion off")
        if after.get("occlusion_enabled") is not True:
            raise ValueError(f"{name}: experiment must use --occlusion on")
        if not after.get("occlusion_ready"):
            raise ValueError(f"{name}: GPU does not have working occlusion queries")
        deltas = {
            field: round(float(after[field]) - float(baseline[field]), 4)
            for field in ("paint_cpu_ms_median", "paint_ms_median",
                          "frame_ms_median", "frame_ms_p95")
        }
        results.append({
            "map": name,
            "delta_ms_on_minus_off": deltas,
            "cpu_overhead_ms_median": after.get(
                "occlusion_cpu_overhead_ms_median"),
            "cpu_index_ms_median": after.get(
                "occlusion_cpu_index_ms_median"),
            "probes_mean": after.get("occlusion_probes_issued_mean"),
            "probes_max": after.get("occlusion_probes_issued_max"),
            "skipped_brushes_mean": after.get("occlusion_skipped_mean"),
            "cpu_benefit": deltas["paint_cpu_ms_median"] < 0,
            "frame_benefit": deltas["frame_ms_median"] < 0,
        })
    if len(results) != len(off):
        raise ValueError("Different map sets in off and on runs")
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("off", type=Path, help="off.json from --occlusion off")
    parser.add_argument("on", type=Path, help="on.json from --occlusion on")
    args = parser.parse_args(argv)
    with args.off.open(encoding="utf-8") as fh:
        off = json.load(fh)
    with args.on.open(encoding="utf-8") as fh:
        on = json.load(fh)
    for entry in compare(off, on):
        delta = entry["delta_ms_on_minus_off"]
        print(f"{entry['map']}:")
        print(f"  CPU submission median delta: "
              f"{delta['paint_cpu_ms_median']:+.4f} ms "
              f"({'faster' if entry['cpu_benefit'] else 'not faster'})")
        print(f"  Total frame median delta:  "
              f"{delta['frame_ms_median']:+.4f} ms "
              f"({'faster' if entry['frame_benefit'] else 'not faster'})")
        print(f"  Frame p95 delta:           "
              f"{delta['frame_ms_p95']:+.4f} ms")
        print(f"  Query draws: mean {entry['probes_mean']}, "
              f"maximum {entry['probes_max']}")
        print(f"  Skipped brushes/frame: {entry['skipped_brushes_mean']}")
    print("Repeat with the same map and camera script on the SQ3: a single "
          "run is not sufficient for statistical confidence.")


if __name__ == "__main__":
    main()
