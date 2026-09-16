"""Project Overview — what is actually in this map, counted.

This used to be a collapsible panel pinned to the bottom third of the Scene
Hierarchy, where it competed for height with the thing people open the
hierarchy to use: the list. It is a report, not a workspace, so it is a dialog
off the Tools menu now and the hierarchy is the list again.

The counting lives in :func:`collect_project_stats`, apart from the dialog, so
what a map contains can be asserted in a test without building a window.
"""

import datetime
import os

from PyQt5.QtWidgets import QMessageBox

from editor.things import Light, Model, Monster, Portal

try:
    from editor.io_system import get_connections
    IO_AVAILABLE = True
except ImportError:                                      # pragma: no cover
    IO_AVAILABLE = False


def _is_mover(brush) -> bool:
    """Whether a brush moves under its own power.

    A door *is* a mover — the same animation, the same Open/Close/Stop inputs,
    with a lock and a use prompt on top — so counting the two separately
    reported a map as having no movers when half its geometry moved.
    """
    return bool(brush.get('is_mover', False) or brush.get('is_door', False))


def _casts_shadows(thing) -> bool:
    """Whether a light is a shadow caster, which is the expensive kind.

    The count worth having: an ordinary light is close to free, and one with a
    depth cube-map behind it is not, so "how many lights" answers a much less
    useful question than "how many of them cost something".
    """
    if not isinstance(thing, Light):
        return False
    value = thing.properties.get('casts_shadows', False)
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'on')
    return bool(value)


def collect_project_stats(state, file_path=None) -> dict:
    """Everything the overview reports, as plain data.

    *file_path* is the map on disk, used only for the timestamps; a map that
    has never been saved simply has none.
    """
    brushes = list(getattr(state, 'brushes', ()) or ())
    things = list(getattr(state, 'things', ()) or ())

    connections = 0
    if IO_AVAILABLE:
        for entity in brushes + things:
            connections += len(get_connections(entity))

    stats = {
        'brushes': len(brushes),
        'things': len(things),
        'movers': sum(1 for b in brushes if _is_mover(b)),
        'triggers': sum(1 for b in brushes if b.get('is_trigger', False)),
        'lights': sum(1 for t in things if isinstance(t, Light)),
        'dynamic_lights': sum(1 for t in things if _casts_shadows(t)),
        'models': sum(1 for t in things if isinstance(t, Model)),
        'monsters': sum(1 for t in things if isinstance(t, Monster)),
        'portals': sum(1 for t in things if isinstance(t, Portal)),
        'connections': connections,
    }
    stats.update(_collect_timestamps(state, file_path))
    return stats


def _collect_timestamps(state, file_path) -> dict:
    """``created`` and ``modified``, as display strings.

    ``created`` is the timestamp the map file carries, written on its first
    save. Maps written before that existed have none, so the file's own
    creation time stands in — and where the filesystem cannot answer that
    either (most of Linux does not record it), it says so rather than quietly
    showing the modification time twice.
    """
    created = getattr(state, 'created_at', '') or ''
    if created:
        created = _pretty(created)

    modified = ''
    if file_path and os.path.exists(file_path):
        try:
            modified = _pretty_epoch(os.path.getmtime(file_path))
        except OSError:
            modified = ''
        if not created:
            try:
                birth = getattr(os.stat(file_path), 'st_birthtime', None)
                created = _pretty_epoch(birth) if birth else 'unknown'
            except (OSError, AttributeError):
                created = 'unknown'

    return {
        'created': created or 'not saved yet',
        'modified': modified or 'not saved yet',
    }


def _pretty(iso_text: str) -> str:
    try:
        return datetime.datetime.fromisoformat(iso_text).strftime('%d %b %Y, %H:%M')
    except (TypeError, ValueError):
        return str(iso_text)


def _pretty_epoch(seconds: float) -> str:
    return datetime.datetime.fromtimestamp(seconds).strftime('%d %b %Y, %H:%M')


#: Rows of the report, in display order. ``None`` is a separator.
OVERVIEW_ROWS = [
    ("Brushes", 'brushes'),
    ("Things", 'things'),
    None,
    ("Movers", 'movers'),
    ("Triggers", 'triggers'),
    ("Portals", 'portals'),
    None,
    ("Lights", 'lights'),
    ("Dynamic lights", 'dynamic_lights'),
    ("Models", 'models'),
    ("Monsters", 'monsters'),
    None,
    ("I/O connections", 'connections'),
]


def format_overview(stats: dict, map_name: str) -> str:
    """The report as rich text for a message box."""
    rows = []
    for row in OVERVIEW_ROWS:
        if row is None:
            rows.append('<tr><td colspan="2"><hr style="border:1px solid #444;">'
                        '</td></tr>')
            continue
        label, key = row
        rows.append(
            '<tr><td style="padding:2px 18px 2px 0; color:#aaaaaa;">%s</td>'
            '<td style="padding:2px 0; color:#ffffff;"><b>%s</b></td></tr>'
            % (label, stats.get(key, 0)))

    return (
        '<div style="font-size:11pt;"><b style="color:#F08000;">%s</b></div>'
        '<table style="margin-top:8px;">%s</table>'
        '<hr style="border:1px solid #444; margin-top:10px;">'
        '<div style="color:#aaaaaa;">Created: %s</div>'
        '<div style="color:#aaaaaa;">Last modified: %s</div>'
        % (map_name, ''.join(rows), stats['created'], stats['modified'])
    )


def show_project_overview(main_window):
    """Open the Project Overview for the map the editor currently has open."""
    file_path = getattr(main_window, 'file_path', None)
    map_name = os.path.basename(file_path) if file_path else 'Untitled'
    stats = collect_project_stats(main_window.state, file_path)

    box = QMessageBox(main_window)
    box.setWindowTitle("Project Overview")
    box.setTextFormat(1)            # Qt.RichText
    box.setText(format_overview(stats, map_name))
    box.setStandardButtons(QMessageBox.Ok)
    box.exec_()
