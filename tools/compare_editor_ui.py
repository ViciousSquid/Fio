"""Join native Windows/macOS editor captures into a reviewable parity report."""

import argparse
import csv
import html
import json
from pathlib import Path
import shutil
from collections import Counter


def compare_frames(windows, mac):
    """Return measured differences; native font/theme differences are visible,
    and controls larger or newly clipped on macOS are ranked separately."""
    result = []
    win = {w["key"]: w for w in windows["widgets"]}
    cocoa = {w["key"]: w for w in mac["widgets"]}
    # Cocoa and Windows can insert different anonymous wrapper widgets into
    # the same wizard header. Match unique controls by their own identity
    # before calling them missing; still compare every font and rectangle.
    def identity(widget):
        return widget['class'], widget.get('name', ''), widget.get('text', '')
    win_only = {key: value for key, value in win.items() if key not in cocoa}
    mac_only = {key: value for key, value in cocoa.items() if key not in win}
    win_counts = Counter(identity(value) for value in win_only.values())
    mac_counts = Counter(identity(value) for value in mac_only.values())
    mac_keys = {identity(value): key for key, value in mac_only.items()}
    for key, value in win_only.items():
        signature = identity(value)
        if win_counts[signature] == mac_counts[signature] == 1:
            cocoa[key] = cocoa.pop(mac_keys[signature])
    for key in sorted(win.keys() | cocoa.keys()):
        a, b = win.get(key), cocoa.get(key)
        if a is None or b is None:
            result.append({"kind": "missing-control", "key": key,
                           "text": (a or b).get("text", ""),
                           "windows": bool(a), "macos": bool(b)})
            continue
        shared = {"key": key, "text": a.get("text", ""), "class": a["class"]}
        if a.get("shorter_than_font") is False and b.get("shorter_than_font") is True:
            result.append(dict(shared, kind="new-text-height-clipping",
                               windows=a["rect"][3], macos=b["rect"][3]))
        for field in ("family", "point_size", "resolved_point_size", "pixel_size", "weight", "italic", "height", "sample_width"):
            if a["font"].get(field) != b["font"].get(field):
                result.append(dict(shared, kind="font-" + field,
                                   windows=a["font"].get(field), macos=b["font"].get(field)))
        aw, ah = a["rect"][2:]
        bw, bh = b["rect"][2:]
        button = "Button" in a["class"] or a["class"] in ("QCheckBox", "QRadioButton")
        if button and ah > 0 and bh - ah >= 4 and bh / ah > 1.2:
            result.append(dict(shared, kind="oversized-macos-control-height", windows=ah, macos=bh))
        if button and aw > 0 and bw - aw >= 12 and bw / aw > 1.3:
            result.append(dict(shared, kind="oversized-macos-control-width", windows=aw, macos=bw))
        if max(abs(x - y) for x, y in zip(a["rect"], b["rect"])) >= 4:
            result.append(dict(shared, kind="geometry", windows=a["rect"], macos=b["rect"]))
        if a.get("tabs") != b.get("tabs") and (a.get("tabs") or b.get("tabs")):
            result.append(dict(shared, kind="tab-geometry", windows=a.get("tabs"), macos=b.get("tabs")))
    return result


def build_report(captures, output):
    output.mkdir(parents=True, exist_ok=True)
    manifests = []
    for path in sorted(captures.rglob("manifest.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("platform") in ("win32", "darwin"):
            manifests.append((path, data))
    grouped = {}
    for path, data in manifests:
        key = (data["font_size"], data["scale_factor"])
        grouped.setdefault(key, {})[data["platform"]] = (path, data)
    for font_size in (11, 16):
        for scale in ("1", "2"):
            grouped.setdefault((font_size, scale), {})
    summary = {"pairs": [], "capture_errors": [], "missing_platforms": [], "findings": {}}
    frames_html, all_diffs = [], []
    for (font_size, scale), platforms in sorted(grouped.items()):
        label = "font-" + str(font_size) + "-scale-" + str(scale)
        if "win32" not in platforms or "darwin" not in platforms:
            summary["missing_platforms"].append(label)
            continue
        wp, wd = platforms["win32"]
        mp, md = platforms["darwin"]
        for side, manifest in (("windows", wd), ("macos", md)):
            summary["capture_errors"].extend(dict(e, platform=side, config=label)
                                             for e in manifest["errors"])
            if not manifest.get("capture_complete", True):
                summary["capture_errors"].append({"surface": "audit-driver", "platform": side,
                    "config": label, "traceback": "Capture terminated before visiting all surfaces"})
        wf, mf = ({f["id"]: f for f in d["frames"]} for d in (wd, md))
        pair = {"config": label, "windows_frames": len(wf), "macos_frames": len(mf),
                "matched_frames": len(wf.keys() & mf.keys()),
                "missing_windows": sorted(mf.keys() - wf.keys()),
                "missing_macos": sorted(wf.keys() - mf.keys()),
                "windows": {k: wd.get(k) for k in ("commit", "qt_platform", "style", "logical_dpi", "native_dpr",
                                                   "available_geometry")},
                "macos": {k: md.get(k) for k in ("commit", "qt_platform", "style", "logical_dpi", "native_dpr",
                                                 "available_geometry")}}
        summary["pairs"].append(pair)
        if wd["commit"] != md["commit"]:
            raise ValueError("The operating systems captured different commits")
        for frame_id in sorted(wf.keys() & mf.keys()):
            a, b = wf[frame_id], mf[frame_id]
            diffs = compare_frames(a, b)
            for diff in diffs:
                diff.update(config=label, frame=frame_id)
                summary["findings"][diff["kind"]] = summary["findings"].get(diff["kind"], 0) + 1
            all_diffs.extend(diffs)
            images = []
            for name, parent, frame in (("Windows", wp.parent, a), ("macOS", mp.parent, b)):
                if not frame["image"]:
                    continue
                relative = Path("images") / label / name / frame["image"]
                destination = output / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(parent / frame["image"], destination)
                images.append('<figure><figcaption>' + name + ' ' + str(frame["size"]) +
                              '</figcaption><img loading="lazy" src="' + relative.as_posix() + '"></figure>')
            if not images:
                continue
            priority = [d for d in diffs if d["kind"].startswith(("oversized", "new-text", "missing"))]
            rows = "".join("<tr><td>" + html.escape(d["kind"]) + "</td><td>" +
                           html.escape(d.get("text", "")[:80]) + "</td><td>" +
                           html.escape(str(d["windows"])) + "</td><td>" +
                           html.escape(str(d["macos"])) + "</td></tr>" for d in (priority + diffs)[:70])
            title = label + " / " + frame_id
            frames_html.append('<details data-search="' + html.escape(title.lower(), quote=True) +
                '"><summary>' + html.escape(title) + ' — ' + str(len(diffs)) +
                ' differences, ' + str(len(priority)) + ' priority</summary><div class="pair">' +
                "".join(images) + '</div><table><tr><th>Finding</th><th>Control</th><th>Windows</th>' +
                '<th>macOS</th></tr>' + rows + '</table></details>')
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    (output / "differences.json").write_text(json.dumps(all_diffs, indent=2, ensure_ascii=False), encoding="utf-8")
    font_pairs = Counter((d["kind"], str(d["windows"]), str(d["macos"]))
                         for d in all_diffs if d["kind"].startswith("font-"))
    summary["common_font_differences"] = [
        {"metric": kind, "windows": win, "macos": mac, "occurrences": count}
        for (kind, win, mac), count in font_pairs.most_common(40)]
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    fields = ("config", "frame", "kind", "key", "class", "text", "windows", "macos")
    with (output / "differences.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(all_diffs)
    heading = """<!doctype html><html lang="en"><meta charset="utf-8">
<title>Fio — Windows / macOS editor parity</title>
<style>body{font:15px system-ui;background:#17191d;color:#e2e6ed;margin:24px}
input{width:95%;padding:12px;margin:12px 0;background:#292d33;color:white;border:1px solid #667}
details{border:1px solid #484e59;padding:12px;margin:8px 0}summary{cursor:pointer}
.pair{display:flex;gap:16px;align-items:flex-start}figure{margin:12px 0;flex:1;min-width:0}
img{width:100%;height:auto}td,th{text-align:left;padding:6px;border-bottom:1px solid #454950}
table{width:100%;font-size:12px}pre{white-space:pre-wrap}</style>
<h1>Fio — Windows / macOS editor parity</h1>
<p>Shared application styles on native Windows and Cocoa Qt; identical source, scene and settings.
The requested editor size is 1440×900;
actual logical window and panel sizes after native layout are recorded for every frame.
Images are normalized to logical pixels. Resolved fonts, DPI and native DPR are in the manifests.
Font fallback and native theme differences are reported, not silently treated as equivalent.
Qt file/colour pickers are included. 3D rendering, gameplay HUD and arbitrary field-value combinations are excluded.</p>
<p>All measured differences are in differences.csv and differences.json. Priority findings are
new text-height clipping, missing controls, and controls enlarged on macOS. A pixel-perfect
pass is not implied by a successful capture job.</p><input id="search" placeholder="Filter by dialog, entity, tab or configuration">
"""
    page = heading + "<pre>" + html.escape(json.dumps(summary, indent=2)) + "</pre>" + "".join(frames_html)
    page += """<script>document.querySelector('#search').addEventListener('input',e=>{
const q=e.target.value.toLowerCase();document.querySelectorAll('details').forEach(d=>
d.hidden=!d.dataset.search.includes(q))})</script></html>"""
    (output / "index.html").write_text(page, encoding="utf-8")
    # Compact, machine-readable evidence is also retained in the Actions log.
    print("UI_PARITY_SUMMARY " + json.dumps(summary, separators=(",", ":")), flush=True)
    priorities = [d for d in all_diffs if d["kind"].startswith(("oversized", "new-text", "missing"))]
    print("UI_PARITY_PRIORITY " + json.dumps(priorities[:100], separators=(",", ":")), flush=True)
    # Remaining review observations, one compact record per finding, so the
    # log alone shows which surfaces still differ without the report ZIP.
    review = [{k: d.get(k) for k in ("config", "frame", "kind", "class", "key", "text", "windows", "macos")}
              for d in all_diffs if d not in priorities]
    for diff in review[:400]:
        diff["text"] = (diff["text"] or "")[:40]
        print("UI_PARITY_REVIEW " + json.dumps(diff, separators=(",", ":"), ensure_ascii=False), flush=True)
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--captures", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = build_report(args.captures, args.output)
    critical = any(count and kind.startswith(('font-', 'tab-geometry', 'oversized-', 'new-text-height-clipping'))
                   for kind, count in result['findings'].items())
    return 1 if (critical or not result["pairs"] or result["capture_errors"] or result["missing_platforms"]) else 0


if __name__ == "__main__":
    raise SystemExit(main())
