            "min_frame_time_ms": float(np.min(values)),
            "max_frame_time_ms": float(np.max(values)),
            "average_fps": (1000.0 / avg_ms) if avg_ms > 0.0 else 0.0,
            "wall_clock_fps": (frame_count / duration) if duration > 0.0 else 0.0,
            "average_visible_tris": avg_visible,
            "average_total_tris": avg_total,
            "average_culled_tris": avg_culled,
            "culling_efficiency": efficiency,
        }
    def _report_current_world_combined_if_complete(self):
        expected = int(self._requested_repetitions) * 2
        if len(self._current_phase_results) != expected:
            return

        total_frames = sum(int(r.get("frame_count", 0)) for r in self._current_phase_results)
        total_frame_time_ms = sum(
            float(r.get("average_frame_time_ms", 0.0)) * int(r.get("frame_count", 0))
            for r in self._current_phase_results
        )
        if total_frames <= 0 or total_frame_time_ms <= 0.0:
            return

        combined_fps = 1000.0 * total_frames / total_frame_time_ms
        total_seconds = total_frame_time_ms / 1000.0
        self._append(
            '<div style="margin:14px 0 10px 0; padding:10px 0; border-top:2px solid #555; border-bottom:2px solid #555;">'
            '<div style="display:flex; align-items:center; justify-content:flex-start; gap:10px; white-space:nowrap;">'
            '<span style="display:inline-block; width:7px; height:42px; background:#ff9a32; flex:none;"></span>'
            '<span style="font-size:25px; font-weight:bold; color:#63d471;">Combined Current World Average FPS:</span>'
            '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32;">%.2f FPS</span>'
            '</div>'
            '<div style="color:#aaa; padding:4px 0 0 17px;">Phase 1 + Phase 2 across %d run%s • %.2f seconds • %d captured frames</div>'
            '</div>' % (
                combined_fps, int(self._requested_repetitions),
                "" if int(self._requested_repetitions) == 1 else "s",
                total_seconds, total_frames
            )
        )
        self._results.append({
            "test": "current_world_combined",
            "description": "Combined Phase 1 + Phase 2 Current World result",
            "average_fps": combined_fps,
            "average_frame_time_ms": 1000.0 / combined_fps,
            "frame_count": total_frames,
            "measurement_duration_s": total_seconds,
            "benchmark_combined": True,
            "benchmark_repetitions": int(self._requested_repetitions),
        })

    def _report_live_result(self, label, metrics):
        width = metrics.get("viewport_width", self.main_window.view_3d.width())
        height = metrics.get("viewport_height", self.main_window.view_3d.height())
        avg_fps = float(metrics.get("average_fps", 0.0))
        avg_ms = float(metrics.get("average_frame_time_ms", 0.0))
        p95_ms = float(metrics.get("p95_frame_time_ms", 0.0))
        p99_ms = float(metrics.get("p99_frame_time_ms", 0.0))
        p999_ms = float(metrics.get("p999_frame_time_ms", 0.0))
        frames = int(metrics.get("frame_count", 0))
        duration = float(metrics.get("measurement_duration_s", 0.0))
        low_1 = 1000.0 / p99_ms if p99_ms > 0.0 else 0.0
        low_01 = 1000.0 / p999_ms if p999_ms > 0.0 else 0.0
        result = dict(metrics)
        display_label = metrics.get("benchmark_phase_label", label)
        result.update({"test": label, "description": display_label, "entities": len(self.main_window.state.things), "one_percent_low_fps": low_1, "zero_point_one_percent_low_fps": low_01})
        self._results.append(result)
        self.export_button.setEnabled(True)
        self.export_button.setVisible(True)
        self.output.append('<div style="background:#222; border:1px solid #555; padding:12px; margin:4px 0 10px 0;">'
                           '<div style="font-size:15px; font-weight:bold; color:#eeeeee; margin-bottom:4px;">%s</div>'
                           '<div style="color:#aaa;">%dx%d &nbsp; • &nbsp; %.2f ms average frame &nbsp; • &nbsp; %.2f ms median &nbsp; • &nbsp; %.2f ms p95</div>'
                           '<div style="color:#aaa;">%d frames &nbsp; • &nbsp; %.2f s measured &nbsp; • &nbsp; wall-clock %.2f FPS &nbsp; • &nbsp; 1%% low %.2f FPS &nbsp; • &nbsp; 0.1%% low %.2f FPS</div>'
                           '<div style="color:#aaa;">VRAM %s &nbsp; • &nbsp; brushes %d visible / %d culled / %d total &nbsp; • &nbsp; entities %d</div>'
                           '</div>' % (display_label, width, height, avg_ms, float(metrics.get("median_frame_time_ms", 0.0)), p95_ms, frames, duration, float(metrics.get("wall_clock_fps", 0.0)), low_1, low_01, self._format_vram(metrics), metrics.get("visible_brushes", 0), metrics.get("culled_brushes", 0), metrics.get("total_brushes", 0), result["entities"]))
        if label in ("current_world", "borderless_window", "fullscreen_window", "editor_windowed_1280", "editor_windowed_1920"):
            sweep_line = (
                '<div style="padding:4px 0; color:#aaa;">'
                '<b style="color:#eeeeee;">Camera sweep:</b> %s'
                ' &nbsp; • &nbsp; <b style="color:#eeeeee;">anchor:</b> %s%s'
                '</div>'
                % (
                    metrics.get("camera_sweep_mode", "player-area"),
                    metrics.get("camera_sweep_anchor", "unknown"),
                    " &nbsp; • &nbsp; <b style=\"color:#ff8a00;\">FALLBACK: %s</b>"
                    % str(metrics.get("camera_sweep_fallback_reason", "unknown"))
                    if metrics.get("camera_sweep_fallback") else "",
                )
            )
            self.output.append(sweep_line)
            self.output.append('<div style="padding:4px 0;">'
                               '<span style="color:#eeeeee; font-weight:bold;">Average visible triangles: </span><span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                               '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Average total triangles: </span><span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                               '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Average culled triangles: </span><span style="color:#ff9a32; font-weight:bold;">%.0f</span>'
                               '<span style="color:#eeeeee; font-weight:bold;"> &nbsp; • &nbsp; Culling efficiency: </span><span style="color:#ff9a32; font-weight:bold;">%.1f%%</span></div>'
                               % (metrics.get("average_visible_tris", 0.0), metrics.get("average_total_tris", 0.0), metrics.get("average_culled_tris", 0.0), metrics.get("culling_efficiency", 0.0)))
        self.output.append('<div style="margin-top:10px; padding:8px 0 2px 0; border-top:2px solid #555; white-space:nowrap;">'
                           '<span style="font-size:25px; font-weight:bold; color:#63d471;">average FPS:</span>'
                           '<span style="font-size:42px; line-height:1; font-weight:bold; color:#ff9a32; margin-left:12px;">%.2f</span>'
                           '</div>' % avg_fps)
        self.output.append('<div style="color:#aaa; padding:2px 0 4px 0;">Average FPS = 1000 / mean(captured frame time). Wall-clock FPS is reported separately.</div>')
        self.output.append('<div style="border-top:2px solid #63d471; margin:14px 0 8px 0;"></div>')
        self.output.ensureCursorVisible()
        QApplication.processEvents()
    @staticmethod
    def _format_vram(metrics):
        used = metrics.get("vram_used_mb")
        total = metrics.get("vram_total_mb")
        if used is None and total is None:
            return "N/A"
        if used is None or total is None:
            return "%s / %s MB" % (used, total)
        return "%.1f / %.1f MB" % (used, total)

    def _restore_original(self, failed=False):
        if self._restoring:
            return
        self._restoring = True
        try:
            if self.main_window.view_3d.play_mode:
                self.main_window._exit_play_mode()