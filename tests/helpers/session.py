"""A real Fio session a test can step: thin orchestration, no fakes.

::

    session = FioTestSession(qt_app, "maps/_SHOWCASE.json")
    session.start_play()
    session.step(10, keys={Qt.Key_W})
    with session.render_state() as frame:
        assert frame.entity_table.count == len(session.state.things)

Everything underneath is production code: ``MainWindow`` loads the map,
``enter_play_mode`` starts the session, ``LogicThread._step_frame`` runs the
ticks and projects the frame, ``ThreadedGameState`` publishes it, and
``QtGameView.paintGL`` draws it. Only the environment is controlled:

* **time** - the logic thread's free-running loop is halted and ticks are
  stepped explicitly, one fixed tick at a time;
* **the monster AI's scheduling** - the same ``MonsterAI.update`` its thread
  runs, under the same lock, every other tick (its 30 Hz against the logic's
  60 Hz), so a run does the same work every time;
* **input** - written to ``ThreadedGameState`` where Qt's key handlers put it;
* **the display** - offscreen Qt, or Xvfb for :meth:`paint`.

The working directory is the repository root, as for ``main.py``: the engine
resolves ``assets/`` against it. The editor writes its layout and recent files
back to the repository's ``settings.ini``; the session restores the file when
it closes, so a session used outside pytest leaves no trace either.
"""

import contextlib
import os
import time

from tests.helpers.paths import REPO_ROOT


class FioTestSession:
    def __init__(self, app, level, *, size=(320, 180)):
        """Open the editor on *level*: a repo-relative map path or level data."""
        from editor.main_window import MainWindow

        self.app = app
        self._previous_cwd = os.getcwd()
        os.chdir(REPO_ROOT)
        self._settings_path = os.path.join(REPO_ROOT, "settings.ini")
        with open(self._settings_path, "rb") as handle:
            self._settings = handle.read()
        self.window = MainWindow(REPO_ROOT)
        self.window.show()
        self.view = self.window.view_3d
        self.view.resize(*size)
        if app.platformName() == "offscreen":
            # No GL context, so initializeGL (which starts the logic thread
            # after building the renderer) never runs: start it the same way.
            self.view._start_logic_thread()
        self._pump_until(lambda: self.view.logic_thread is not None)
        self.logic = self.view.logic_thread
        # Halted, not replaced: the object, its runtimes and its lock are the
        # ones the editor wired up; only its loop no longer runs on its own.
        self.logic.running = False
        self.logic.join(5.0)
        assert not self.logic.is_alive(), "logic thread did not stop"
        self.tick_index = 0
        if isinstance(level, str):
            assert self.window.load_level_file(os.path.join(REPO_ROOT, level)), level
        else:
            assert self.window._load_level(level), "level was refused"
        self.app.processEvents()

    # -- the live objects -------------------------------------------------

    @property
    def state(self):
        return self.window.state

    @property
    def game_state(self):
        return self.view.game_state

    @property
    def playing(self):
        return bool(self.view.play_mode)

    # -- play ---------------------------------------------------------------

    def start_play(self):
        self.window.enter_play_mode()
        assert self.playing, "play mode did not start"
        self.logic.session_runtime.stop_monster_ai()
        return self

    def stop_play(self):
        if self.playing:
            self.window.enter_play_mode()
        assert not self.playing

    def step(self, ticks=1, *, keys=(), mouse=(0.0, 0.0), shoot=False, use=False):
        """Run *ticks* fixed logic ticks with this input held, publishing each."""
        logic, game_state = self.logic, self.game_state
        for _ in range(ticks):
            game_state.set_keys(set(keys))
            game_state.set_mouse_delta(*mouse)
            if shoot:
                game_state.queue_shot()
            if use:
                game_state.set_use_key_pressed()
            logic._step_frame(logic.TICK_DURATION)
            if self.playing and self.tick_index % 2 == 0:
                with logic.session_runtime.monster_lock:
                    logic.monster_ai.update(2.0 * logic.TICK_DURATION)
            logic._publish_frame()
            self.tick_index += 1
        return self

    def publish(self):
        """Project and publish the world as it is, without advancing time."""
        self.logic._step_frame(0.0)
        self.logic._publish_frame()
        return self

    @contextlib.contextmanager
    def render_state(self):
        """Borrow the newest published frame, as the renderer does."""
        game_state = self.game_state
        game_state.try_swap()
        frame = game_state.get_render_state()
        try:
            yield frame
        finally:
            game_state.release_render_state(frame)

    def paint(self):
        """Draw the newest frame through ``QtGameView.paintGL``; return RGB pixels.

        Needs a real OpenGL context (a display; Xvfb in CI).
        """
        import numpy as np
        from OpenGL import GL

        view = self.view
        self.game_state.try_swap()
        view.makeCurrent()
        try:
            view.paintGL()
            GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, view.defaultFramebufferObject())
            scale = view.devicePixelRatioF()
            width, height = int(view.width() * scale), int(view.height() * scale)
            GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
            data = GL.glReadPixels(0, 0, width, height, GL.GL_RGB, GL.GL_UNSIGNED_BYTE)
            GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 4)
        finally:
            view.doneCurrent()
        image = np.frombuffer(data, np.uint8).reshape(height, width, 3)
        return image[::-1].copy()

    # -- teardown -----------------------------------------------------------

    def close(self):
        """Stop Play, close the editor, and make sure it is really gone.

        A closed window that stays reachable keeps its LogicThread's tables
        subscribed to the process-wide change journal, where they collect
        every change the next session makes. The window's own deferred timers
        hold it until the event loop has run them, so events are processed
        until the window is collected; one still alive after that is a leak.
        """
        import gc
        import weakref
        from PyQt5.QtCore import QEvent
        try:
            self.stop_play()
        finally:
            window = weakref.ref(self.window)
            self.window.unsaved_changes = False
            self.window.close()
            self.window.deleteLater()
            self.window = self.view = self.logic = None
            with open(self._settings_path, "wb") as handle:
                handle.write(self._settings)
            os.chdir(self._previous_cwd)
            deadline = time.perf_counter() + 2.0
            while window() is not None and time.perf_counter() < deadline:
                self.app.processEvents()
                self.app.sendPostedEvents(None, QEvent.DeferredDelete)
                gc.collect()
                time.sleep(0.01)
            assert window() is None, "a closed FioTestSession's window is still reachable"

    def _pump_until(self, predicate, timeout=20.0):
        deadline = time.perf_counter() + timeout
        while not predicate():
            assert time.perf_counter() < deadline, "editor did not finish starting"
            self.app.processEvents()
            time.sleep(0.005)
