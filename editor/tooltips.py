"""Showing and hiding a panel's tooltips.

Qt keeps a tooltip on the widget it belongs to, so switching them off means
taking the text away and being able to give it back.  The original is parked
in a Qt dynamic property, which travels with the widget: a panel rebuilt
around a stashed widget still knows what its tooltip said.

Both switches are per-area rather than global because the two areas are read
very differently -- toolbar tooltips are how you learn the icons, and stop
being wanted long before the Property Editor's do.
"""

from PyQt5.QtWidgets import QWidget

#: Dynamic property holding the real tooltip while it is hidden.
STASH = 'fio_stashed_tooltip'


def set_tooltips_enabled(root, enabled):
    """Show or hide every tooltip under ``root``, including ``root``'s own.

    Safe to call repeatedly and in either order: hiding twice keeps the first
    stash rather than overwriting it with the blank, and showing a widget that
    was never hidden leaves it alone.
    """
    if root is None:
        return
    for widget in [root] + root.findChildren(QWidget):
        set_widget_tooltip_enabled(widget, enabled)


def set_widget_tooltip_enabled(widget, enabled):
    """Show or hide one widget's tooltip."""
    if enabled:
        stashed = widget.property(STASH)
        if stashed:
            widget.setToolTip(stashed)
            widget.setProperty(STASH, None)
        return

    tip = widget.toolTip()
    if tip:
        widget.setProperty(STASH, tip)
        widget.setToolTip('')
