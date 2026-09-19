                getattr(self.main_window, "unsaved_changes", False)
            )
            camera = self.main_window.view_3d.camera
            self._original_window_flags = self.main_window.windowFlags()
            self._original_window_geometry = self.main_window.geometry()
            self._original_window_state = self.main_window.windowState()
            self._original_window_fullscreen = self.main_window.isFullScreen()

            self._original_camera = (
                (float(camera.pos.x), float(camera.pos.y), float(camera.pos.z)),
                float(camera.yaw),
                float(camera.pitch),
                float(camera.fov),
            )

            self._queue = []
            repetitions = max(1, int(self._requested_repetitions))
            self._current_phase_results = []

            # Phase 1/2 are specifically benchmarks of the currently loaded
            # world. Never enqueue them for an empty editor; optional stress
            # tests are independent and may still run without a loaded map.
            if has_current_map:
                for repetition in range(1, repetitions + 1):
                    self._queue.append(("current_world_phase1", repetition))
                    self._queue.append(("current_world_phase2", repetition))

            if self.additional_tests.isChecked():
                self._queue.extend([
                    ("procedural_100_monsters", 100),
                    ("live_io_1000", 1000),
                ])
            if self.io_chain_1000.isChecked():
                self._queue.append(("live_io_1000", 1000))
            if self.monsters_100.isChecked():
                self._queue.append(("procedural_100_monsters", 100))
            if self.monsters_500.isChecked():
                self._queue.append(("procedural_500_monsters", 500))
            if self.monsters_1000.isChecked():
                self._queue.append(("procedural_1000_monsters", 1000))
            if self.monster_apocalypse.isChecked():
                self._queue.append(("monster_apocalypse", 1000))