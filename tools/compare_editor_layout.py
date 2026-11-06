"""Fail when the editor's widgets differ between platforms.

Each platform's screenshot job records every visible control of the editor
window (``editor_ui_audit.Audit.widgets``) at one fixed client size. This
compares those records: the same controls and docks, with the same text,
enabled state and font. Dock rectangles must be identical; control and tab
rectangles and measured text metrics may differ only by ``--tolerance``
pixels, because each OS's font engine rounds the same font differently.
Native window frames are outside the client area and are not compared.

    python tools/compare_editor_layout.py captures/ --tolerance 1 \
        --expect windows macos linux-x11 linux-wayland
"""

import argparse
import json
import os
from pathlib import Path
import sys

FONT_FIELDS = ("family", "point_size", "resolved_point_size", "pixel_size", "weight", "italic")
#: Measured by each OS's font engine, which rounds the same font differently.
FONT_METRICS = ("height", "ascent", "sample_width")


def load(root):
    """``{platform: record}`` for every layout record under *root*."""
    records = {}
    for path in sorted(Path(root).rglob("*-layout.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        records[data["platform"]] = data
    return records


def _near(a, b, tolerance):
    """Equal, or numbers (and lists of numbers) within *tolerance* pixels."""
    if a == b:
        return True
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) <= tolerance
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        return all(_near(x, y, tolerance) for x, y in zip(a, b))
    if isinstance(a, dict) and isinstance(b, dict) and a.keys() == b.keys():
        return all(_near(a[k], b[k], tolerance) if k == "rect" else a[k] == b[k] for k in a)
    return False


def compare(reference, other, tolerance=0, rounded=None):
    """Every difference between two layout records, as readable strings.

    Control rectangles, tab rectangles and font-engine metrics may differ by
    up to *tolerance* pixels (font rounding); those are appended to
    *rounded* instead. Everything else must be identical.
    """
    rounded = [] if rounded is None else rounded
    if reference["size"] != other["size"]:
        return [f"window client size {reference['size']} != {other['size']}"]
    ours = {w["key"]: w for w in reference["widgets"]}
    theirs = {w["key"]: w for w in other["widgets"]}
    found = []
    docks_a, docks_b = reference.get("docks", {}), other.get("docks", {})
    for name in sorted(docks_a.keys() | docks_b.keys()):
        if docks_a.get(name) != docks_b.get(name):
            found.append(f"dock {name}: {docks_a.get(name)} != {docks_b.get(name)}")
    for key in sorted(ours.keys() - theirs.keys()):
        found.append(f"{key} ({ours[key].get('text', '')!r}): only on {reference['platform']}")
    for key in sorted(theirs.keys() - ours.keys()):
        found.append(f"{key} ({theirs[key].get('text', '')!r}): only on {other['platform']}")
    for key in sorted(ours.keys() & theirs.keys()):
        a, b = ours[key], theirs[key]
        label = f"{key} ({a.get('text', '')!r})"
        for field in ("text", "enabled"):
            if a.get(field) != b.get(field):
                found.append(f"{label}: {field} {a.get(field)} != {b.get(field)}")
        for field in ("rect", "tabs"):
            if a.get(field) != b.get(field):
                line = f"{label}: {field} {a.get(field)} != {b.get(field)}"
                (rounded if _near(a.get(field), b.get(field), tolerance) else found).append(line)
        for field in FONT_FIELDS:
            if a["font"].get(field) != b["font"].get(field):
                found.append(f"{label}: font {field} {a['font'].get(field)} != {b['font'].get(field)}")
        for field in FONT_METRICS:
            if a["font"].get(field) != b["font"].get(field):
                line = f"{label}: font {field} {a['font'].get(field)} != {b['font'].get(field)}"
                near = _near(a["font"].get(field), b["font"].get(field), tolerance)
                (rounded if near else found).append(line)
    return found


def report(records, expect=(), tolerance=0):
    """``(ok, lines)``: every platform against the first expected one."""
    lines = []
    missing = [name for name in expect if name not in records]
    if missing:
        lines.append("Missing layout records: " + ", ".join(missing))
    if not records:
        return False, lines + ["No layout records were found."]
    order = [name for name in expect if name in records] + sorted(set(records) - set(expect))
    reference = records[order[0]]
    hierarchy = {name: (records[name].get("docks", {}).get("SceneDock") or [None] * 4)[2]
                 for name in order}
    lines.append(f"Reference: {order[0]}, client {reference['size'][0]}x{reference['size'][1]}, "
                 f"{len(reference['widgets'])} controls")
    ok = not missing
    for name in order[1:]:
        rounded = []
        found = compare(reference, records[name], tolerance, rounded)
        ok = ok and not found
        status = "identical" if not found else f"{len(found)} differences"
        if rounded:
            status += f" ({len(rounded)} within {tolerance}px font rounding)"
        lines.append(f"{name}: {status}")
        lines.extend("  " + line for line in found[:200])
        if len(found) > 200:
            lines.append(f"  ... and {len(found) - 200} more")
        lines.extend("  rounding: " + line for line in rounded[:60])
    if any(value is not None for value in hierarchy.values()):
        lines.append("Scene hierarchy width: " + ", ".join(f"{k} {v}" for k, v in hierarchy.items()))
    return ok, lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("captures", help="directory searched for *-layout.json records")
    parser.add_argument("--expect", nargs="*", default=(), help="platforms that must be present")
    parser.add_argument("--tolerance", type=int, default=0,
                        help="pixels of font-engine rounding allowed in control and text metrics")
    args = parser.parse_args(argv)
    ok, lines = report(load(args.captures), args.expect, args.tolerance)
    text = "\n".join(lines)
    print(text)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write("# Editor layout parity\n\n" + ("All platforms identical.\n\n" if ok else "")
                         + "```\n" + text + "\n```\n")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
