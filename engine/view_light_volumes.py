"""Light volumes in the 3D view: each one's box and its light source.

A light volume has no icon or sprite. In the 3D view it is its box, dashed
in the light's colour, and a marker where its light comes from; selected,
the box is solid and a dotted line joins its centre to the source. Drawn
over the frame with the view's QPainter (as the transform gizmo is), so
every stroke is triangles and no GL line is wider than one pixel.
"""

from __future__ import annotations

import numpy as np
from PyQt5.QtCore import QPointF, Qt
from PyQt5.QtGui import QColor, QPen

#: The light-source marker's radius, pixels.
SOURCE_MARKER_PX = 6.0
#: How near (pixels) a click must be to a light volume's source marker.
SOURCE_PICK_PX = 9.0


def light_volumes(view):
    """The light volumes the 3D view shows (not hidden, not filtered)."""
    from editor.things import LightVolume
    filters = getattr(view.editor.state, 'view_filters', None)
    out = []
    for thing in view.editor.state.things:
        if not isinstance(thing, LightVolume):
            continue
        if thing.properties.get('hidden', False):
            continue
        if filters is not None and filters.hides_thing(thing):
            continue
        out.append(thing)
    return out


def box_edges(volume):
    """The twelve world-space edges of a light volume's box."""
    lo, hi = volume.get_bounds()
    corners = [np.array((x, y, z), dtype=float)
               for x in (lo[0], hi[0]) for y in (lo[1], hi[1]) for z in (lo[2], hi[2])]
    return [(corners[a], corners[b]) for a in range(8) for b in range(a + 1, 8)
            if bin(a ^ b).count('1') == 1]


def source_hit(view, volume, x, y):
    """Whether widget pixel ``(x, y)`` is on *volume*'s source marker."""
    project = view.gizmo._project_fn()
    p = project(volume.get_source_position())
    return p is not None and abs(p[0] - x) <= SOURCE_PICK_PX and abs(p[1] - y) <= SOURCE_PICK_PX


def paint(view, painter):
    volumes = light_volumes(view)
    if not volumes:
        return
    project = view.gizmo._project_fn()
    selected = set(map(id, view.editor.selected_objects_list()))
    painter.save()
    painter.setRenderHint(painter.Antialiasing, True)
    try:
        for volume in volumes:
            r, g, b = (max(0, min(255, int(c * 255))) for c in volume.get_color()[:3])
            chosen = id(volume) in selected
            pen = QPen(QColor(r, g, b, 255 if chosen else 150), 1.6 if chosen else 1.0)
            pen.setStyle(Qt.SolidLine if chosen else Qt.DashLine)
            painter.setPen(pen)
            painter.setBrush(Qt.NoBrush)
            for a, b_ in box_edges(volume):
                pa, pb = project(a), project(b_)
                if pa is not None and pb is not None:
                    painter.drawLine(QPointF(*pa), QPointF(*pb))
            source = project(volume.get_source_position())
            if source is None:
                continue
            if chosen:
                centre = project(volume.pos)
                if centre is not None:
                    dotted = QPen(QColor(255, 220, 0), 1.0)
                    dotted.setStyle(Qt.DotLine)
                    painter.setPen(dotted)
                    painter.drawLine(QPointF(*centre), QPointF(*source))
                painter.setPen(QPen(QColor(0, 0, 0), 1.0))
                painter.setBrush(QColor(255, 220, 0))
            else:
                painter.setPen(QPen(QColor(r, g, b, 200), 1.0))
                painter.setBrush(QColor(r, g, b, 90))
            rad = SOURCE_MARKER_PX if chosen else SOURCE_MARKER_PX * 0.75
            painter.drawEllipse(QPointF(*source), rad, rad)
            if chosen:
                painter.drawLine(QPointF(source[0] - rad, source[1]), QPointF(source[0] + rad, source[1]))
                painter.drawLine(QPointF(source[0], source[1] - rad), QPointF(source[0], source[1] + rad))
    finally:
        painter.restore()
