"""Fail when the editor's widgets differ between platforms.

Each platform's screenshot job records every visible control of the editor
window (``editor_ui_audit.Audit.widgets``) at one fixed client size. This
compares those records strictly: the same controls, at the same rectangles,
with the same text, enabled state, resolved font and tab geometry. Native
window frames are outside the client area and are not compared.

    python tools/compare_editor_layout.py captures/ --expect windows macos linux-x11 linux-wayland
"""

import argparse
import json
import os
from pathlib import Path
import sys

FONT_FIELDS = ("family", "point_size", "resolved_point_size", "pixel_size", "weight",
               "italic", "height", "ascent", "sample_width")


def load(root):
    """``{platform: record}`` for every layout record under *root*."""
    records = {}
    for path in sorted(Path(root).rglob("*-layout.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        records[data["platform"]] = data
    return records


def compare(reference, other):
    """Every difference between two layout records, as readable strings."""
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
        for field in ("rect", "text", "enabled", "tabs"):
            if a.get(field) != b.get(field):
                found.append(f"{label}: {field} {a.get(field)} != {b.get(field)}")
        for field in FONT_FIELDS:
            if a["font"].get(field) != b["font"].get(field):
                found.append(f"{label}: font {field} {a['font'].get(field)} != {b['font'].get(field)}")
    return found


def report(records, expect=()):
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
        found = compare(reference, records[name])
        ok = ok and not found
        lines.append(f"{name}: " + ("identical" if not found else f"{len(found)} differences"))
        lines.extend("  " + line for line in found[:200])
        if len(found) > 200:
            lines.append(f"  ... and {len(found) - 200} more")
    if any(value is not None for value in hierarchy.values()):
        lines.append("Scene hierarchy width: " + ", ".join(f"{k} {v}" for k, v in hierarchy.items()))
    return ok, lines


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("captures", help="directory searched for *-layout.json records")
    parser.add_argument("--expect", nargs="*", default=(), help="platforms that must be present")
    args = parser.parse_args(argv)
    ok, lines = report(load(args.captures), args.expect)
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
