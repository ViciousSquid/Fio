"""ThreadedGameState tells a listener each time it publishes a frame.

Kiosk mode paints each frame the moment it is published instead of on the
view's 16 ms timer, which polled a 60 Hz publisher on its own clock and
showed a frame twice about three times a second. Both ways a frame is
published -- the logic thread's swap, and a held-back frame published when
the renderer lets go of the read buffer -- notify, outside the lock, and a
failing listener never reaches the publisher.
"""

from engine.threaded_game_state import ThreadedGameState


def _listening():
    state = ThreadedGameState()
    calls = []

    def listener():
        # Never called with the render-state lock held: a listener may well
        # look at the state (try_swap) and must not deadlock.
        assert state._render_state_lock.holder is None
        calls.append(state.published_frames)
    state.publish_listener = listener
    return state, calls


def test_a_published_frame_is_announced():
    state, calls = _listening()
    assert state.request_swap() is True
    assert calls == [1]


def test_a_declined_swap_is_not_announced_until_the_renderer_lets_go():
    state, calls = _listening()
    state.request_swap()
    snapshot = state.get_render_state()        # the renderer is reading
    assert state.request_swap() is False
    assert calls == [1]
    state.release_render_state(snapshot)       # the held frame goes out now
    assert calls == [1, 2]


def test_a_failing_listener_never_reaches_the_publisher():
    state = ThreadedGameState()

    def broken():
        raise RuntimeError("listener failed")
    state.publish_listener = broken
    assert state.request_swap() is True
    assert state.try_swap() is True


def test_no_listener_by_default():
    state = ThreadedGameState()
    assert state.publish_listener is None
    assert state.request_swap() is True
