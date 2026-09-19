"""Benchmark result aggregation, HTML reporting and metadata."""

import configparser
from datetime import datetime, timezone
import html
import os
import platform
import subprocess
from PyQt5.QtWidgets import QApplication, QFileDialog


def _execution_environment():
    """Return a human-readable Python/CPU execution mode."""
    import platform

    process_arch = platform.machine() or "unknown"
    host_arch = process_arch
    translation = "none detected"

    if sys.platform == "win32":
        try:
            import ctypes
            process = ctypes.windll.kernel32.GetCurrentProcess()
            process_machine = ctypes.c_ushort()
            native_machine = ctypes.c_ushort()
            fn = ctypes.windll.kernel32.IsWow64Process2
            fn.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ushort), ctypes.POINTER(ctypes.c_ushort)]
            fn.restype = ctypes.c_bool
            if fn(process, ctypes.byref(process_machine), ctypes.byref(native_machine)):
                names = {0x014C: "x86", 0x8664: "x64", 0xAA64: "ARM64"}
                process_arch = names.get(process_machine.value, "0x%04X" % process_machine.value)
                host_arch = names.get(native_machine.value, "0x%04X" % native_machine.value)
                if (native_machine.value == 0xAA64 and
                        process_machine.value in (0x014C, 0x8664)):
                    translation = "Microsoft Prism / Windows on ARM emulation"
        except Exception:
            pass
    elif sys.platform == "darwin":
        try:
            import ctypes
            libc = ctypes.CDLL(None)
            translated = ctypes.c_int(0)
            size = ctypes.c_size_t(ctypes.sizeof(translated))
            if libc.sysctlbyname(b"sysctl.proc_translated", ctypes.byref(translated), ctypes.byref(size), None, 0) == 0 and translated.value == 1:
                translation = "Apple Rosetta 2"
                host_arch = "ARM64"
        except Exception:
            pass

    return "Python: %s | Host CPU: %s | Translation: %s" % (
        process_arch, host_arch, translation
    )


class BenchmarkResults:
    def __init__(self, runner):
        object.__setattr__(self, "runner", runner)

    def __getattr__(self, name):
        return getattr(self.runner, name)

    def __setattr__(self, name, value):
        if name == "runner":
            object.__setattr__(self, name, value)
        else:
            setattr(self.runner, name, value)

def _vsync_metadata(self):
    """Return the Fio display VSync setting used by the Qt application."""
    settings_path = os.path.join(self.root_dir, "settings.ini")
    configured = True
    source = "default (settings.ini missing or Display.vsync absent)"
    try:
        parser = configparser.ConfigParser()
        parser.read(settings_path, encoding="utf-8")
        if parser.has_option("Display", "vsync"):
            configured = parser.getboolean("Display", "vsync")
            source = "settings.ini [Display] vsync"
    except (configparser.Error, ValueError, OSError):
        source = "default (could not read settings.ini [Display].vsync)"

    return {
        "enabled": bool(configured),
        "swap_interval": 1 if configured else 0,
        "source": source,
    }


def _git_commit(self):
    try:
        return subprocess.check_output(["git", "-C", self.root_dir, "rev-parse", "HEAD"], stderr=subprocess.DEVNULL, text=True, timeout=2).strip()
    except Exception:
        return "unknown"

@staticmethod

def _html_escape(value):
    return html.escape(str(value), quote=True)


def _benchmark_results_html(self, version):
    timestamp = datetime.now(timezone.utc).isoformat()
    environment = self._html_escape(_execution_environment())
    platform_name = self._html_escape(platform.platform())
    python_version = self._html_escape(platform.python_version())
    cpu = self._html_escape(platform.processor() or "unknown")
    commit = self._html_escape(self._git_commit())
    vsync = self._vsync_metadata()
    vsync_state = "Enabled" if vsync["enabled"] else "Disabled"
    vsync_interval = int(vsync["swap_interval"])
    vsync_source = self._html_escape(vsync["source"])
    version = self._html_escape(version)

    cards = []
    for result in self._results:
        label = self._html_escape(result.get("test", "benchmark"))
        if result.get("aborted"):
            reason = self._html_escape(result.get("abort_reason", "No reason supplied"))
            status_text = (
                "ABORTED — timeout"
                if "timeout" in str(result.get("abort_reason", "")).lower()
                else "ABORTED — worker failure"
            )
            cards.append(
                '<section class="result aborted">'
                '<h2>%s</h2>'
                '<div class="abort">%s</div>'
                '<p>%s</p>'
                '</section>' % (label, self._html_escape(status_text), reason)
            )
            continue

        description = self._html_escape(result.get("description", result.get("test", "")))
        fps = result.get("average_fps")
        mean_ms = result.get("average_frame_time_ms", result.get("mean_ms"))
        p95_ms = result.get("p95_frame_time_ms", result.get("p95_ms"))
        resolution = self._html_escape(result.get("resolution", ""))
        brushes = result.get("brush_count", result.get("brushes"))
        entities = result.get("entity_count", result.get("entities"))

        metrics = []
        if resolution:
            metrics.append("Resolution: %s" % resolution)
        if fps is not None:
            metrics.append("Average FPS: %.2f (from captured frame time)" % float(fps))
        if "wall_clock_fps" in result:
            metrics.append("Wall-clock FPS: %.2f (captured frames / measurement duration)" % float(result["wall_clock_fps"]))
        if mean_ms is not None:
            metrics.append("Average frame: %.2f ms" % float(mean_ms))
        if p95_ms is not None:
            metrics.append("p95: %.2f ms" % float(p95_ms))
        if brushes is not None:
            metrics.append("Brushes: %s" % self._html_escape(brushes))
        if entities is not None:
            metrics.append("Entities: %s" % self._html_escape(entities))
        if "hops_per_second" in result:
            metrics.append("I/O: %.0f hops/s" % float(result["hops_per_second"]))
        if "clip_operations_per_second" in result:                metrics.append("CSG: %.0f clip operations/s" % float(result["clip_operations_per_second"]))

        fps_html = ""
        if fps is not None:
            fps_html = '<div class="fps">%.2f <span>FPS</span></div>' % float(fps)

        extra = []
        sysmon = result.get("sysmon") or {}
        visible_tris = result.get("average_visible_tris", sysmon.get("average_visible_tris"))
        total_tris = result.get("average_total_tris", sysmon.get("average_total_tris"))
        culled_tris = result.get("average_culled_tris", sysmon.get("average_culled_tris"))
        culling_efficiency = result.get("culling_efficiency", sysmon.get("culling_efficiency"))
        if visible_tris is not None:
            extra.append("Average visible triangles: %.0f" % float(visible_tris))
        if total_tris is not None:
            extra.append("Average total triangles: %.0f" % float(total_tris))
        if culled_tris is not None:
            extra.append("Average culled triangles: %.0f" % float(culled_tris))
        if culling_efficiency is not None:
            extra.append("Culling efficiency: %.1f%%" % float(culling_efficiency))
        if "one_percent_low_fps" in result:
            extra.append("1% low: %.2f FPS" % float(result["one_percent_low_fps"]))
        if "zero_point_one_percent_low_fps" in result:
            extra.append("0.1% low: %.2f FPS" % float(result["zero_point_one_percent_low_fps"]))
        vram_source = result if ("vram_used_mb" in result or "vram_total_mb" in result) else sysmon
        if vram_source:
            extra.append("VRAM: %s" % self._html_escape(self._format_vram(vram_source)))
        if "final_camera_pos" in result:
            extra.append("Final camera position: %s" % self._html_escape(result["final_camera_pos"]))
        if result.get("camera_sweep_mode"):
            extra.append(
                "Camera sweep: %s" % self._html_escape(
                    result["camera_sweep_mode"]
                )
            )
            extra.append(
                "Sweep anchor: %s" % self._html_escape(
                    result.get("camera_sweep_anchor", "unknown")
                )
            )
            if result.get("camera_sweep_fallback"):
                extra.append(
                    "Sweep fallback: %s" % self._html_escape(
                        result.get("camera_sweep_fallback_reason", "unknown")
                    )
                )
            if result.get("camera_sweep_reachable_cells") is not None:
                extra.append(
                    "Reachable flood cells: %s" % self._html_escape(
                        result["camera_sweep_reachable_cells"]
                    )
                )

        details_html = ""
        if extra:
            details_html = '<div class="details">%s</div>' % "<br>".join(self._html_escape(item) for item in extra)

        metrics_html = "<span> • </span>".join(self._html_escape(item) for item in metrics)
        cards.append(
            '<section class="result">'
            '<div class="result-head"><div><h2>%s</h2><p>%s</p></div>%s</div>'
            '<div class="metrics">%s</div>%s'
            '</section>' % (label, description, fps_html, metrics_html, details_html)
        )

    return """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Fio Benchmark Report</title>
<style>
body { margin:0; padding:32px; background:#111; color:#ddd; font-family:Segoe UI,Arial,sans-serif; }
main { max-width:1100px; margin:0 auto; }
h1 { margin:0 0 8px; color:#eee; font-size:30px; }
h2 { margin:0; color:#eee; font-size:18px; }
p { margin:5px 0 0; color:#aaa; }
.meta { margin:0 0 24px; padding:16px; background:#191919; border:1px solid #333; line-height:1.7; font-family:Consolas,monospace; font-size:13px; }
.result { margin:14px 0; padding:18px; background:#1b1b1b; border:1px solid #3a3a3a; border-radius:6px; }
.result-head { display:flex; justify-content:space-between; gap:20px; align-items:flex-start; }
.fps { color:#ff9a32; font-size:34px; font-weight:700; white-space:nowrap; }
.fps span { color:#63d471; font-size:16px; }
.metrics { margin-top:14px; color:#bbb; line-height:1.8; }
.details { margin-top:12px; color:#eee; line-height:1.8; }
.aborted { border-color:#ff8a00; background:#21180f; }
.abort { margin-top:10px; color:#ff8a00; font-size:24px; font-weight:700; }
.footer { margin-top:28px; padding-top:14px; border-top:2px solid #63d471; color:#777; font-size:12px; }
</style>
</head>
<body><main>
<h1>Fio Benchmark Report</h1>
<div class="meta">
Fio version: %s<br>
Generated: %s<br>
Execution: %s<br>
Average FPS definition: 1000 / mean(captured frame time)<br>
VSync: <strong>%s</strong> (swap interval %d)<br>
VSync source: %s<br>
<div style="margin:8px 0; padding:8px; color:#aaa; background:#151515; border-left:3px solid #63d471;">Live editor/window tests use this VSync setting. Isolated stress workers use independent GL test contexts, so their renderer FPS is not capped by the editor's presentation VSync.</div>
Platform: %s<br>
Python: %s<br>
CPU: %s<br>
Git commit: %s
</div>
%s
<div class="footer">Generated by Fio Tools &gt; Benchmark. Benchmark workloads run against the existing Fio MainWindow; timeout supervision never terminates Fio.</div>
</main></body>
</html>""" % (
        version, self._html_escape(timestamp), environment, vsync_state,
        vsync_interval, vsync_source, platform_name, python_version, cpu,
        commit, "".join(cards)
    )


def _export_results(self):
    if not self._results:
        return
    default_name = "fio_benchmark_%s.html" % datetime.now().strftime("%Y%m%d_%H%M%S")
    path, _ = QFileDialog.getSaveFileName(
        self,
        "Export Fio Benchmark Results",
        default_name,
        "HTML files (*.html);;All files (*)",
    )
    if not path:
        return
    version = "unknown"
    version_path = os.path.join(self.root_dir, "editor", "version.txt")
    try:
        with open(version_path, "r", encoding="utf-8") as f:
            version = f.read().strip()
    except Exception:
        pass
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self._benchmark_results_html(version))
        self.status_label.setText("Exported benchmark report: %s" % os.path.basename(path))
    except Exception:
        self._append("<span style='color:#ff6666;'>Export failed.</span><pre>%s</pre>" % traceback.format_exc())


def _report_worker_result(self, label, payload):
    """Append results returned by an isolated benchmark worker."""
    results = payload.get("results", [])
    if not results:
        raise RuntimeError(
            "isolated worker %s returned no benchmark results" % label
        )

    for result in results:
        result = dict(result)
        result["worker_test"] = label
        self._results.append(result)
        self.export_button.setEnabled(True)
        self.export_button.setVisible(True)

        average_fps = result.get("average_fps")
        resolution = result.get("resolution", "")
        entities = result.get("entity_count", result.get("entities"))
        brushes = result.get("brush_count", result.get("brushes"))
        description = result.get("description", label)

        self.output.append(
            '<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
            '<div style="font-size:15px; font-weight:bold; color:#eeeeee; margin-bottom:4px;">%s</div>'
            '<div style="color:#aaa;">%s%s%s%s</div>'
            '</div>'
            % (
                result.get("test", label),
                description,
                (" &nbsp; • &nbsp; " + resolution) if resolution else "",
                (" &nbsp; • &nbsp; " + str(brushes) + " brushes") if brushes is not None else "",
                (" &nbsp; • &nbsp; " + str(entities) + " entities") if entities is not None else "",
            )
        )

        metrics = result.get("sysmon") or {}
        if average_fps is not None:
            self.output.append(
                '<table cellspacing="0" cellpadding="0" style="margin-top:10px; margin-bottom:2px;">'
                '<tr>'
                '<td width="24" rowspan="2" bgcolor="#63d471"></td>'
                '<td height="2" bgcolor="#63d471" style="font-size:2px; line-height:2px;"></td>'
                '</tr>'
                '<tr>'
                '<td style="padding:6px 16px 2px 12px; white-space:nowrap;">'
                '<span style="font-size:25px; font-weight:bold; color:#63d471;">Average FPS:</span>'
                '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f FPS</span>'
                '</td>'
                '</tr>'
                '</table>' % float(average_fps)
            )
            self.output.append(
                '<div style="color:#aaa; padding:4px 0;">frame time %.2f ms &nbsp; • &nbsp; p95 %.2f ms%s</div>'
                % (
                    float(metrics.get("average_frame_time_ms", result.get("mean_ms", 0.0))),
                    float(metrics.get("p95_frame_time_ms", result.get("p95_ms", 0.0))),
                    (" &nbsp; • &nbsp; VRAM " + self._format_vram(metrics)) if metrics else "",
                )
            )
        if "hops_per_second" in result:
            self.output.append(
                '<div style="color:#aaa; padding:4px 0;">I/O throughput: <b style="color:#ff9a32;">%.0f hops/s</b></div>'
                % float(result["hops_per_second"])
            )
        if "clip_operations_per_second" in result:
            self.output.append(                    '<div style="color:#aaa; padding:4px 0;">CSG throughput: <b style="color:#ff9a32;">%.0f clip operations/s</b></div>'
                % float(result["clip_operations_per_second"])
            )
        if "final_camera_pos" in result:
            self.output.append(
                '<div style="color:#aaa; padding:4px 0;">Final camera position: %s</div>'
                % (result["final_camera_pos"],)
            )

    self.output.append('<div style="border-top:2px solid #63d471; margin:14px 0 8px 0;"></div>')
    self.output.ensureCursorVisible()
    QApplication.processEvents()


def _benchmark_metrics(capture, duration_s):
    """Reduce a SysMon benchmark capture to stable, reportable metrics."""
    frame_times = []
    for value in capture.get("frame_times", []):
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(value) and value > 0.0:
            frame_times.append(value)

    elapsed = max(0.0, float(duration_s))
    if frame_times:
        ordered = sorted(frame_times)
        mean_ms = sum(frame_times) / len(frame_times)
        p95_index = int(math.ceil(0.95 * len(ordered))) - 1
        p95_ms = ordered[max(0, min(len(ordered) - 1, p95_index))]
        average_fps = 1000.0 / mean_ms if mean_ms > 0.0 else 0.0
        wall_clock_fps = len(frame_times) / elapsed if elapsed > 0.0 else 0.0
    else:
        mean_ms = 0.0
        p95_ms = 0.0
        average_fps = 0.0
        wall_clock_fps = 0.0

    visible = [float(v) for v in capture.get("visible_tris", [])]
    total = [float(v) for v in capture.get("total_tris", [])]
    culled = [float(v) for v in capture.get("culled_tris", [])]

    avg_visible = sum(visible) / len(visible) if visible else 0.0
    avg_total = sum(total) / len(total) if total else 0.0
    avg_culled = sum(culled) / len(culled) if culled else 0.0

    sysmon = {
        "average_frame_time_ms": mean_ms,
        "p95_frame_time_ms": p95_ms,
        "average_visible_tris": avg_visible,
        "average_total_tris": avg_total,
        "average_culled_tris": avg_culled,
        "culling_efficiency": (
            (avg_culled / avg_total) * 100.0 if avg_total > 0.0 else 0.0
        ),
    }

    return {
        "average_fps": average_fps,
        "wall_clock_fps": wall_clock_fps,
        "average_frame_time_ms": mean_ms,
        "p95_frame_time_ms": p95_ms,
        "mean_ms": mean_ms,
        "p95_ms": p95_ms,
        "sample_count": len(frame_times),
        "measurement_duration_s": elapsed,
        "average_visible_tris": avg_visible,
        "average_total_tris": avg_total,
        "average_culled_tris": avg_culled,
        "culling_efficiency": sysmon["culling_efficiency"],
        "sysmon": sysmon,
    }

@staticmethod

def _format_vram(metrics):
    used = metrics.get("vram_used_mb")
    total = metrics.get("vram_total_mb")
    if used is None and total is None:
        return "N/A"
    if used is None:
        return "%.0f MB total" % float(total)
    if total is None:
        return "%.0f MB used" % float(used)
    return "%.0f / %.0f MB" % (float(used), float(total))


def _report_live_result(self, label, metrics):
    """Record and display a completed live benchmark measurement."""
    result = dict(metrics)
    result["test"] = label
    result["description"] = label.replace("_", " ").title()
    result["benchmark_live"] = True
    result["resolution"] = "%dx%d" % (
        int(metrics.get("viewport_width", self.main_window.view_3d.width())),
        int(metrics.get("viewport_height", self.main_window.view_3d.height())),
    )
    self._results.append(result)
    self.export_button.setEnabled(True)
    self.export_button.setVisible(True)

    average_fps = float(result.get("average_fps", 0.0))
    self.output.append(
        '<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
        '<div style="font-size:15px; font-weight:bold; color:#eeeeee; margin-bottom:4px;">%s</div>'
        '<div style="color:#aaa;">%s &nbsp; • &nbsp; %s</div>'
        '<table cellspacing="0" cellpadding="0" style="margin-top:10px; margin-bottom:2px;">'
        '<tr><td width="24" rowspan="2" bgcolor="#63d471"></td>'
        '<td height="2" bgcolor="#63d471" style="font-size:2px; line-height:2px;"></td></tr>'
        '<tr><td style="padding:6px 16px 2px 12px; white-space:nowrap;">'
        '<span style="font-size:25px; font-weight:bold; color:#63d471;">Average FPS:</span>'
        '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f FPS</span>'
        '</td></tr></table>'
        '<div style="color:#aaa; padding:4px 0;">frame time %.2f ms &nbsp; • &nbsp; p95 %.2f ms%s</div>'
        '</div>'
        % (
            self._html_escape(label),
            self._html_escape(result.get("description", label)),
            self._html_escape(result.get("resolution", "")),
            average_fps,
            float(result.get("average_frame_time_ms", 0.0)),
            float(result.get("p95_frame_time_ms", 0.0)),
            (
                " &nbsp; • &nbsp; VRAM " + self._format_vram(result)
                if result.get("vram_used_mb") is not None or result.get("vram_total_mb") is not None
                else ""
            ),
        )
    )
    self.output.ensureCursorVisible()
    QApplication.processEvents()


def _report_current_world_combined_if_complete(self):
    """Report the combined result after a PlayerStart phase pair."""
    if len(self._current_phase_results) < 2:
        return

    phase1 = self._current_phase_results[-2]
    phase2 = self._current_phase_results[-1]
    if phase1.get("benchmark_phase") != 1 or phase2.get("benchmark_phase") != 2:
        return
    if phase1.get("benchmark_repetition") != phase2.get("benchmark_repetition"):
        return

    duration = (
        float(phase1.get("measurement_duration_s", 0.0))
        + float(phase2.get("measurement_duration_s", 0.0))
    )
    samples = (
        int(phase1.get("sample_count", 0))
        + int(phase2.get("sample_count", 0))
    )
    combined_fps = samples / duration if duration > 0.0 else 0.0
    result = {
        "test": "current_world_combined",
        "description": "Combined PlayerStart benchmark",
        "benchmark_live": True,
        "benchmark_phase_label": "Phase 1 orbit + Phase 2 360° rotation",
        "benchmark_repetition": phase1.get("benchmark_repetition"),
        "average_fps": combined_fps,
        "sample_count": samples,
        "measurement_duration_s": duration,
        "average_frame_time_ms": (1000.0 / combined_fps if combined_fps > 0.0 else 0.0),
        "p95_frame_time_ms": max(
            float(phase1.get("p95_frame_time_ms", 0.0)),
            float(phase2.get("p95_frame_time_ms", 0.0)),
        ),
        "resolution": phase1.get("resolution", ""),
    }
    self._results.append(result)
    self.export_button.setEnabled(True)
    self.export_button.setVisible(True)
    self._append(
        '<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
        '<div style="font-size:15px; font-weight:bold; color:#eeeeee;">Combined PlayerStart benchmark</div>'
        '<div style="color:#aaa; padding:4px 0;">Phase 1 orbit + Phase 2 360° rotation</div>'
        '<table cellspacing="0" cellpadding="0" style="margin-top:10px;">'
        '<tr><td width="24" rowspan="2" bgcolor="#63d471"></td>'
        '<td height="2" bgcolor="#63d471" style="font-size:2px; line-height:2px;"></td></tr>'
        '<tr><td style="padding:6px 16px 2px 12px; white-space:nowrap;">'
        '<span style="font-size:25px; font-weight:bold; color:#63d471;">Combined Average FPS:</span>'
        '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f FPS</span>'
        '</td></tr></table></div>' % combined_fps
    )
    QApplication.processEvents()

