        """Finish a live stress measurement and cleanly leave Play Mode."""
        view = self.main_window.view_3d
        self._measurement_active = False
        self._timer.stop()
        self._stop_live_stress_monitor()
        metrics = dict(metrics)
        metrics["benchmark_live"] = True

        if label == "live_io_1000":
            hops = len([
                thing for thing in self.main_window.state.things
                if str(thing.properties.get("name", "")).startswith("BenchmarkRelay_")
            ])
            elapsed = float(self._live_io_elapsed or 0.0)
            metrics.update({
                "io_elapsed_ms": elapsed * 1000.0,
                "io_hops": hops,
                "hops_per_second": hops / elapsed if elapsed > 0.0 else 0.0,
            })
            self._append(                "  Live I/O throughput: %.0f hops/s."
                % metrics["hops_per_second"]
            )

        if label.startswith(("procedural_", "monster_")):
            pos = view.camera.pos
            self._append(
                "  Play Mode: god_mode=True, AI active, infighting active, "
                "final camera=(%.1f, %.1f, %.1f)"
                % (float(pos.x), float(pos.y), float(pos.z))
            )
            self._bench.finish_live_monster_test(self.main_window)
        elif label == "live_io_1000" and view.play_mode:
            self.main_window._exit_play_mode()
            QApplication.processEvents()

        if label == "live_io_1000":
            # I/O is not a rendering benchmark. Do not present an FPS card for
            # it; show the completed hop count, elapsed time and throughput in
            # the dedicated I/O result format.
            elapsed_ms = float(metrics.get("io_elapsed_ms", 0.0))
            hops = int(metrics.get("io_hops", 0))
            hops_per_second = float(metrics.get("hops_per_second", 0.0))
            self._results.append(metrics)
            self.export_button.setEnabled(True)
            self.export_button.setVisible(True)
            self.output.append(
                '<div style="padding:12px 0 22px 38px;">'
                '<div style="font-size:27px; font-weight:bold; color:#2db34a; '
                'padding-bottom:13px; border-bottom:3px solid #2db34a; '
                'width:347px; white-space:nowrap;">'
                '%d hops took <span style="color:#ff7f20;">%.3f ms</span>'
                '</div>'
                '<table cellspacing="0" cellpadding="0" '
                'style="margin-top:0;">'
                '<tr><td width="347" height="84" bgcolor="#2db34a" '
                'style="padding:0 0 0 58px; white-space:nowrap;">'
                '<span style="font-size:56px; line-height:1; '
                'font-weight:bold; color:#ff7f20;">%.0f</span>'
                '<span style="font-size:28px; font-weight:bold; '
                'color:#2db34a; margin-left:13px;">hops/second</span>'
                '</td></tr></table>'
                '</div>'
                % (hops, elapsed_ms, hops_per_second)
            )
            self.output.ensureCursorVisible()
            QApplication.processEvents()
            self._begin_next()
            return

        # A live stress test reaches this method directly from _tick, so this
        # method must complete the same two actions as the normal measurement
        # path: publish the result and advance the queue.
        self._report_live_result(label, metrics)
        self._begin_next()