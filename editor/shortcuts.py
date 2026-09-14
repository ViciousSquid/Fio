"""Gathering every keyboard shortcut the editor answers to.

Fio binds keys three different ways, and a list that covered only one of them
would be wrong the day it was written:

* **QAction and QPushButton shortcuts** -- menus and the toolbar.  These are
  read straight off the widgets, so a menu item added tomorrow appears here
  with no extra work.
* **The config file** -- the editable function keys in ``[Shortcuts]``, and
  the user's own key-to-console-command bindings in ``[KeyBindings]``.  Both
  are read live, so what the window shows is what the user has set, not what
  the defaults were.
* **keyPressEvent** -- long if/elif chains in the main window, the 2D views
  and the 3D view.  Nothing can enumerate these, so they are declared in
  :data:`DECLARED` below.  **Adding a key to one of those chains means adding
  a line here too.**

Discovered entries win over declared ones for the same key, so a binding that
moves to a real QAction stops being described by a stale declaration.
"""

from collections import OrderedDict

from PyQt5.QtGui import QKeySequence
from PyQt5.QtWidgets import QAction, QAbstractButton, QShortcut

from editor.tooltips import STASH

#: Categories, in the order the window shows them.
ORDER = (
    'File',
    'Edit',
    'Selection',
    'Component modes',
    'Tools',
    'Brush and texture',
    'View',
    'Grid',
    'Panels',
    'Play mode',
    'Custom bindings',
    'Other',
)

#: Which category a menu belongs to, for shortcuts discovered on QActions.
_MENU_CATEGORY = {
    'file': 'File',
    'edit': 'Edit',
    'select': 'Selection',
    'view': 'View',
    'help': 'Other',
}

#: Shortcuts handled inside a keyPressEvent, which nothing can discover.
#: (category, keys, what it does).  Keep in step with the handlers.
DECLARED = (
    ('Edit', 'Esc', 'Back out: cancel a drag, leave a mode, or deselect'),
    ('Edit', 'Del', 'Delete the selection'),
    ('Edit', 'Ctrl+C', 'Copy the selected brush'),
    ('Edit', 'Ctrl+V', 'Paste, offset by one grid step'),
    ('Edit', 'Shift+Space', 'Clone the selection and place the copy'),
    ('Edit', 'H', 'Hide the selected brush'),
    ('Edit', 'Shift+H', 'Unhide every brush'),

    ('Selection', 'Ctrl+T', 'Select touching'),
    ('Selection', 'Ctrl+I', 'Select inside'),
    ('Selection', 'Ctrl+Shift+T', 'Select partial tall'),
    ('Selection', 'Ctrl+Shift+I', 'Select complete tall'),

    ('Component modes', 'Shift+V', 'Vertex mode'),
    ('Component modes', 'Shift+E', 'Edge mode'),
    ('Component modes', 'Shift+F', 'Face mode'),
    ('Component modes', 'Q', 'Step Object -> Vertex -> Edge -> Face'),

    ('Brush and texture', 'Page Up', "Rotate the hovered face's texture 90 clockwise"),
    ('Brush and texture', 'Page Down', "Rotate the hovered face's texture 90 anticlockwise"),
    ('Brush and texture', 'Enter', 'Apply the clip (in Clip mode)'),
    ('Brush and texture', 'Shift+Enter', 'Apply the clip keeping both sides'),

    ('View', 'Arrow keys', 'Nudge the selection by one grid step'),
    ('View', 'Ctrl+Tab', 'Cycle the 2D view between Top, Front and Side'),
    ('View', '`', 'Toggle the debug console'),

    ('Grid', 'G', 'Show or hide the grid'),
    ('Grid', '[', 'Halve the grid size'),
    ('Grid', ']', 'Double the grid size'),

    ('Play mode', 'W A S D', 'Move'),
    ('Play mode', 'E', 'Use / interact'),
    ('Play mode', 'Esc', 'Leave play mode'),
    ('Play mode', 'F6', 'Toggle model collision'),
    ('Play mode', 'F7', 'Toggle monster debug'),
    ('Play mode', 'F9', 'Toggle the lighting debug view'),
    ('Play mode', 'F12', 'Leave kiosk mode'),
    ('Play mode', '1 2 3 4', 'Select weapon'),
)

#: Config keys in [Shortcuts], with their labels and defaults.
_CONFIG_SHORTCUTS = (
    ('key_show_connections', 'View', 'Show connection lines', 'F1'),
    ('key_toggle_wireframe', 'View', 'Toggle wireframe', 'F2'),
    ('key_sysmon', 'Panels', 'System monitor', 'F3'),
    ('key_play_mode', 'Play mode', 'Enter play mode', 'F5'),
)


class Shortcut:
    """One key combination and what it does."""

    def __init__(self, category, keys, description, source):
        self.category = category
        self.keys = keys
        self.description = description
        self.source = source            # 'menu', 'toolbar', 'config', 'custom', 'built-in'

    def __repr__(self):
        return 'Shortcut(%r, %r, %r)' % (self.category, self.keys, self.description)

    def __eq__(self, other):
        return (isinstance(other, Shortcut)
                and (self.category, self.keys, self.description)
                == (other.category, other.keys, other.description))

    def __hash__(self):
        return hash((self.category, self.keys, self.description))


def _spell(keys):
    """A key combination as Qt writes it, whatever case it arrived in."""
    # An unparseable binding renders as empty rather than raising, so fall
    # back to whatever the config said instead of showing a blank row.
    spelled = QKeySequence(keys).toString()
    return spelled or keys


def _clean(text):
    """A menu label as a person reads it: no ampersands, no trailing dots."""
    return text.replace('&', '').replace('...', '').replace('…', '').strip()


def _label_for(widget):
    """A button's description: its tooltip, or the one tooltips are hiding.

    Settings > Editor > Tooltips can blank a toolbar button's tooltip, which
    would otherwise leave its shortcut here with nothing to call it.
    """
    tip = widget.toolTip() or widget.property(STASH) or ''
    return _clean(tip.split('\n')[0])


def _category_for_action(action, window):
    """Which menu an action sits in, if any.

    An action knows the widgets it was added to, so the menu's own title is
    what sorts it -- no second list of which action belongs where.
    """
    for parent in action.associatedWidgets():
        title = _clean(getattr(parent, 'title', lambda: '')()).lower()
        for name, category in _MENU_CATEGORY.items():
            if title.startswith(name):
                return category
    return None


def _from_actions(window):
    for action in window.findChildren(QAction):
        sequences = [s.toString() for s in action.shortcuts() if not s.isEmpty()]
        if not sequences:
            continue
        label = _clean(action.text()) or _label_for(action)
        if not label:
            continue
        category = _category_for_action(action, window) or 'Tools'
        for keys in sequences:
            yield Shortcut(category, keys, label, 'menu')


def _from_buttons(window):
    for button in window.findChildren(QAbstractButton):
        keys = button.shortcut().toString()
        if not keys:
            continue
        label = _clean(button.text()) or _label_for(button)
        if not label:
            continue
        yield Shortcut('Tools', keys, label, 'toolbar')


def _from_qshortcuts(window):
    for shortcut in window.findChildren(QShortcut):
        keys = shortcut.key().toString()
        label = shortcut.objectName()
        if not keys or not label:
            # Unnamed QShortcuts are described by DECLARED instead; naming
            # them at the point they are created is what lists them here.
            continue
        yield Shortcut('View', keys, _clean(label), 'built-in')


def _from_config(config):
    if config is None:
        return
    for key, category, label, default in _CONFIG_SHORTCUTS:
        keys = default
        if config.has_section('Shortcuts'):
            keys = config.get('Shortcuts', key, fallback=default)
        if keys:
            yield Shortcut(category, keys, label, 'config')

    if config.has_section('KeyBindings'):
        for keys, command in config.items('KeyBindings'):
            # configparser lower-cases its keys, so "Ctrl+9" comes back as
            # "ctrl+9"; QKeySequence puts it back the way Qt spells it.
            yield Shortcut('Custom bindings', _spell(keys),
                           'Console command: %s' % command, 'custom')


def collect(window, config=None):
    """Every shortcut, grouped by category in :data:`ORDER`.

    Returns an ``OrderedDict`` of category to a list of :class:`Shortcut`,
    sorted within each category and with empty categories left out.
    """
    if config is None:
        config = getattr(window, 'config', None)

    found = []
    if window is not None:
        found.extend(_from_actions(window))
        found.extend(_from_buttons(window))
        found.extend(_from_qshortcuts(window))
    found.extend(_from_config(config))

    # A key the editor really binds beats whatever DECLARED says about it.
    discovered = {s.keys.lower() for s in found}
    for category, keys, description in DECLARED:
        if keys.lower() not in discovered:
            found.append(Shortcut(category, keys, description, 'built-in'))

    grouped = OrderedDict()
    seen = set()
    for shortcut in found:
        fingerprint = (shortcut.keys.lower(), shortcut.description.lower())
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        grouped.setdefault(shortcut.category, []).append(shortcut)

    ordered = OrderedDict()
    for category in ORDER:
        if category in grouped:
            ordered[category] = sorted(grouped[category],
                                       key=lambda s: (s.description.lower(), s.keys))
    for category in grouped:                     # anything ORDER did not name
        if category not in ordered:
            ordered[category] = sorted(grouped[category],
                                       key=lambda s: (s.description.lower(), s.keys))
    return ordered


def as_text(grouped):
    """The same listing as plain text, for copying out of the window."""
    lines = []
    for category, shortcuts in grouped.items():
        lines.append(category)
        lines.append('-' * len(category))
        width = max((len(s.keys) for s in shortcuts), default=0)
        for shortcut in shortcuts:
            lines.append('  %-*s  %s' % (width, shortcut.keys, shortcut.description))
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'
