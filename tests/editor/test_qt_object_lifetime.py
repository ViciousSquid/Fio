"""Qt lifetime hazards: when a Python object outlives its C++ half.

PyQt objects have two halves.  Deleting a widget, or deleting its parent,
destroys the C++ one and leaves the Python wrapper behind — alive, referenced,
and fatal to touch.  ``hasattr``/``is not None`` cannot tell the difference;
only ``sip.isdeleted`` can, and code that checks the Python object's existence
instead is the shape of every bug in this file.

The Debug Console and its logger are the case Fio actually hit: both are
process-wide singletons, so once one handed back a destroyed object it did so
for the rest of the session and the console could never be rebuilt.
"""

import pytest

pytest.importorskip("PyQt5", reason="these test PyQt object lifetime directly")

from PyQt5 import sip                                   # noqa: E402
from PyQt5.QtWidgets import QLabel, QVBoxLayout, QWidget  # noqa: E402

from editor import debug_console as dc                  # noqa: E402

pytestmark = pytest.mark.qt


@pytest.fixture(autouse=True)
def _restore_singletons(qt_app):
    """The console and logger are process-wide; put them back afterwards."""
    saved_logger = dc._debug_logger
    saved_logger_instance = dc.DebugLogger._instance
    saved_console_instance = dc.DebugConsole._instance
    yield
    dc._debug_logger = saved_logger
    dc.DebugLogger._instance = saved_logger_instance
    dc.DebugConsole._instance = saved_console_instance


# ---------------------------------------------------------------------------
# The hazard itself
# ---------------------------------------------------------------------------

def test_a_python_wrapper_outlives_the_destroyed_cpp_object(qt_app):
    """The premise every other test here rests on."""
    widget = QWidget()
    sip.delete(widget)

    assert widget is not None, "the Python wrapper is still a live reference"
    assert sip.isdeleted(widget) is True, (
        "sip.isdeleted did not report the destroyed C++ object; the rest of "
        "this module's reasoning would not apply")
    with pytest.raises(RuntimeError):
        widget.isVisible()


def test_deleting_a_parent_destroys_its_children(qt_app):
    """Qt owns its widget tree; a cached child is invalidated by its parent."""
    parent = QWidget()
    child = QLabel("hello", parent)
    assert sip.isdeleted(child) is False

    sip.delete(parent)

    assert sip.isdeleted(child) is True, (
        "the child widget survived its parent's destruction in Python's view; "
        "anything holding it would be about to crash")


def test_a_widget_removed_from_its_layout_is_not_destroyed(qt_app):
    """Removal is not deletion - the difference matters for a page cache."""
    host = QWidget()
    layout = QVBoxLayout(host)
    page = QLabel("page")
    layout.addWidget(page)

    layout.removeWidget(page)

    assert sip.isdeleted(page) is False, (
        "removing a widget from a layout destroyed it; a cached page could "
        "never be put back")


def test_a_widget_removed_from_its_layout_must_be_reparented_or_it_floats(qt_app):
    """A widget with no parent is a top-level window: it shows on the desktop.

    This is the hazard behind a page cache that holds widgets between
    selections - a cached page whose parent is cleared becomes a stray window.
    """
    host = QWidget()
    layout = QVBoxLayout(host)
    page = QLabel("page")
    layout.addWidget(page)
    assert page.parent() is host

    page.setParent(None)
    assert page.parent() is None
    assert page.isWindow() is True, (
        "a parentless widget is a top-level window; caching one in this state "
        "puts a stray panel on the user's desktop")

    page.setParent(host)
    assert page.isWindow() is False


# ---------------------------------------------------------------------------
# The debug logger singleton
# ---------------------------------------------------------------------------

def test_the_logger_is_a_singleton(qt_app):
    assert dc.get_debug_logger() is dc.get_debug_logger()


def test_a_destroyed_logger_is_replaced_rather_than_handed_back(qt_app):
    """The bug: the singleton kept returning a corpse for the rest of the
    process, so every later ``message_logged.connect`` raised and the Debug
    Console could never be built again."""
    logger = dc.get_debug_logger()
    sip.delete(logger)
    assert sip.isdeleted(logger)

    rebuilt = dc.get_debug_logger()

    assert rebuilt is not logger, "the destroyed logger was handed back"
    assert sip.isdeleted(rebuilt) is False
    rebuilt.message_logged.connect(lambda *_a: None)   # must not raise


def test_constructing_the_logger_directly_also_replaces_a_destroyed_one(qt_app):
    """``DebugLogger()`` is the other door into the same singleton."""
    logger = dc.get_debug_logger()
    sip.delete(logger)

    rebuilt = dc.DebugLogger()

    assert sip.isdeleted(rebuilt) is False
    assert rebuilt is dc.DebugLogger._instance


def test_a_rebuilt_logger_starts_with_an_empty_buffer(qt_app):
    logger = dc.get_debug_logger()
    logger.log("Test", "before the crash")
    assert logger.get_buffer()

    sip.delete(logger)
    rebuilt = dc.get_debug_logger()

    assert rebuilt.get_buffer() == [], (
        "the rebuilt logger inherited %d messages from the destroyed one"
        % len(rebuilt.get_buffer()))


def test_logging_through_the_module_helper_survives_a_destroyed_logger(qt_app):
    """``debug_log`` is called from the engine and the AI thread."""
    sip.delete(dc.get_debug_logger())
    dc.debug_log("Test", "after the crash")      # must not raise
    assert any("after the crash" in message
               for _category, message in dc.get_debug_logger().get_buffer())


def test_a_live_logger_is_never_replaced(qt_app):
    logger = dc.get_debug_logger()
    logger.log("Test", "keep me")
    assert dc.get_debug_logger() is logger
    assert logger.get_buffer(), "the live logger lost its buffer"


def test_the_logger_can_be_switched_off(qt_app):
    logger = dc.get_debug_logger()
    logger.clear_buffer()
    logger.set_enabled(False)
    try:
        logger.log("Test", "should be dropped")
        assert logger.get_buffer() == [], (
            "a disabled logger still buffered %s" % (logger.get_buffer(),))
    finally:
        logger.set_enabled(True)
    logger.log("Test", "should be kept")
    assert logger.get_buffer()


def test_the_buffer_is_bounded(qt_app):
    logger = dc.get_debug_logger()
    logger.clear_buffer()
    cap = logger._buffer.maxlen
    assert cap is not None, "the log buffer is unbounded; memory grows for ever"
    for index in range(cap + 50):
        logger.log("Test", "message %d" % index)
    assert len(logger.get_buffer()) == cap


# ---------------------------------------------------------------------------
# The debug console singleton
# ---------------------------------------------------------------------------

def test_a_destroyed_console_is_replaced_rather_than_handed_back(qt_app):
    host = QWidget()
    console = dc.DebugConsole.get_instance(host)
    sip.delete(console)
    assert sip.isdeleted(console)

    rebuilt = dc.DebugConsole.get_instance(host)

    assert rebuilt is not console, "the destroyed console was handed back"
    assert sip.isdeleted(rebuilt) is False


def test_a_console_rebuilt_after_its_logger_died_still_receives_messages(qt_app):
    """The two singletons have to recover together, or the console is deaf."""
    host = QWidget()
    console = dc.DebugConsole.get_instance(host)
    sip.delete(dc.get_debug_logger())
    sip.delete(console)

    rebuilt = dc.DebugConsole.get_instance(host)
    dc.debug_log("Test", "after both were destroyed")

    assert sip.isdeleted(rebuilt) is False
    assert any("after both were destroyed" in message
               for _category, message in dc.get_debug_logger().get_buffer()), (
        "the message never reached the rebuilt logger's buffer")


def test_a_live_console_is_never_replaced(qt_app):
    host = QWidget()
    console = dc.DebugConsole.get_instance(host)
    assert dc.DebugConsole.get_instance(host) is console


# ---------------------------------------------------------------------------
# What a lifetime check must not be
# ---------------------------------------------------------------------------

def test_checking_for_none_does_not_detect_a_destroyed_object(qt_app):
    """Stated outright, because this is the check the bug used.

    A destroyed QObject's wrapper is not None, is truthy, and passes every
    ``hasattr``.  Only ``sip.isdeleted`` separates it from a live one.
    """
    widget = QWidget()
    sip.delete(widget)

    assert widget is not None
    assert hasattr(widget, "isVisible")
    assert sip.isdeleted(widget) is True, (
        "the only check that works did not work")
