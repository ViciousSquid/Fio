"""
Visual node-graph editor for the Fio I/O connection system.

  • Every named entity in the scene gets a node
  • Output pins (right, orange) drag-connect to Input pins (left, blue)
  • Existing _io_connections are drawn automatically on open
  • Right-click any connection to delete or edit its delay / parameter
  • Press Apply (or Ctrl+S) to write connections back to entities

Usage
-----
  from editor.logic_graph_widget import LogicGraphWindow
  win = LogicGraphWindow(editor_state, parent=main_window)
  win.show()
"""

from __future__ import annotations
from typing import Dict, List, Optional, Tuple

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel,
    QGraphicsView, QGraphicsScene, QGraphicsItem,
    QGraphicsEllipseItem, QGraphicsPathItem, QGraphicsRectItem,
    QMenu, QDialog, QFormLayout, QDoubleSpinBox,
    QCheckBox, QLineEdit, QDialogButtonBox, QMessageBox,
    QComboBox, QFrame, QSizePolicy, QToolBar, QAction,
    QGraphicsDropShadowEffect, QApplication, QShortcut
)
from PyQt5.QtCore  import Qt, QRectF, QPointF, pyqtSignal, QSize
from PyQt5.QtGui   import (
    QPainter, QPen, QBrush, QColor, QFont, QPainterPath, QPainterPathStroker,
    QLinearGradient, QIcon, QKeySequence, QFontMetrics
)

try:
    from .io_system import (
        OutputConnection, get_input_names, get_output_names,
        get_entity_type_for_io, IO_REGISTRY
    )
    IO_AVAILABLE = True
except ImportError:
    IO_AVAILABLE = False
    OutputConnection = None


# ── Runtime font / DPI helpers ────────────────────────────────────────────────

def _node_metrics() -> dict:
    """
    Compute node layout dimensions from the live QApplication font.
    Returns a dict with keys: pin_r, nw, nh_hdr, nh_pin, nh_pad.
    """
    fm = QFontMetrics(QApplication.font())
    h  = fm.height()           
    w  = fm.averageCharWidth()
    return {
        'pin_r':  max(5,   h // 3),    
        'nw':     max(200, w * 24),    
        'nh_hdr': h + 16,              
        'nh_pin': h + 10,              
        'nh_pad': 10,                  
    }


def _ui_font(bold: bool = False, delta: int = 0) -> QFont:
    """
    Return a QFont based on QApplication.font().
    """
    f = QFont(QApplication.font())
    if delta:
        f.setPointSize(max(6, f.pointSize() + delta))
    if bold:
        f.setBold(True)
    return f


# ── Colour palette ────────────────────────────────────────────────────────────

C_OUT        = QColor(255, 155,  45)    
C_IN         = QColor( 70, 175, 255)    
C_CONN       = QColor(255, 175,  55, 210)
C_CONN_SEL   = QColor(255, 230, 120, 255)
C_DRAG       = QColor(160, 215, 255, 200)
C_BODY       = QColor( 38,  38,  44)
C_BODY_SEL   = QColor( 55,  55,  64)
C_BORDER     = QColor( 80,  80,  92)
C_BORDER_SEL = QColor(255, 155,  45)

TYPE_HDR: Dict[str, QColor] = {
    'monster':      QColor(185,  55,  55),
    'light':        QColor(205, 170,  35),
    'door':         QColor( 50, 145, 210),
    'mover':        QColor( 80, 130, 210),
    'speaker':      QColor(145,  70, 210),
    'trigger':      QColor(210, 120,  35),
    'logic_relay':  QColor( 50, 185,  95),
    'logic_gate':   QColor( 55, 175,  90),
    'logic_timer':  QColor( 60, 165,  80),
    'pickup':       QColor( 75, 185,  75),
    'playerstart':  QColor( 50, 195, 195),
    'levelchanger': QColor(205,  75, 160),
    'model':        QColor(115, 115, 115),
    'brush':        QColor( 95, 115, 130),
    'portal':       QColor(120,  60, 200),
    'path_node':    QColor( 60, 150, 140),
    'logic_camera': QColor(150,  90, 200),
    'logic_spawner':QColor( 45, 160, 120),
    'logic_state':    QColor(170, 140,  50),
    'logic_keyvalue': QColor(170, 140,  50),   # pre-2.4 token for the same entity
}
C_HDR_DEFAULT = QColor(85, 85, 95)


# ── Pin ───────────────────────────────────────────────────────────────────────

class PinItem(QGraphicsEllipseItem):
    """One input or output pin.

    Its row is not fixed at construction: the node lays its pins out and calls
    :meth:`place`, because which pins are *shown* changes as connections come
    and go (see :meth:`EntityNodeItem.relayout`). A node that drew every
    declared pin at all times would be 23 rows tall for a ``LogicState``, which
    is most of a screen for one entity.
    """

    def __init__(self, node: EntityNodeItem, name: str,
                 is_output: bool, row: int):
        pr = node._pin_r
        super().__init__(-pr, -pr, pr * 2, pr * 2)
        self.node      = node
        self.pin_name  = name
        self.is_output = is_output
        self.row       = row
        self.setParentItem(node)
        self.place(row)

        col = C_OUT if is_output else C_IN
        self.setBrush(QBrush(col))
        self.setPen(QPen(Qt.NoPen))
        self.setZValue(4)
        self.setCursor(Qt.CrossCursor)
        self.setAcceptHoverEvents(True)

    def place(self, row: int):
        """Move this pin to *row*, or hide it when *row* is None."""
        self.row = row
        if row is None:
            self.setVisible(False)
            return
        self.setVisible(True)
        node = self.node
        y = node._nh_hdr + row * node._nh_pin + node._nh_pin // 2
        self.setPos(node._nw if self.is_output else 0, y)

    def scene_center(self) -> QPointF:
        """Where a wire attaches.

        A hidden pin reports its node's header instead of its own position, so a
        wire to a collapsed pin runs to the node rather than to a point floating
        where the pin would have been.
        """
        if not self.isVisible():
            node = self.node
            return node.mapToScene(
                QPointF(node._nw if self.is_output else 0, node._nh_hdr * 0.5))
        return self.mapToScene(QPointF(0, 0))

    def hoverEnterEvent(self, ev):
        self.setBrush(QBrush(QColor(255, 255, 255)))
        self.setPen(QPen(C_OUT if self.is_output else C_IN, 1.5))
        super().hoverEnterEvent(ev)

    def hoverLeaveEvent(self, ev):
        self.setBrush(QBrush(C_OUT if self.is_output else C_IN))
        self.setPen(QPen(Qt.NoPen))
        super().hoverLeaveEvent(ev)


# ── Connection line ───────────────────────────────────────────────────────────

class ConnectionItem(QGraphicsPathItem):
    def __init__(self, src: PinItem, dst: PinItem,
                 conn: Optional[OutputConnection] = None):
        super().__init__()
        self.src_pin  = src
        self.dst_pin  = dst
        self.conn_obj = conn
        self._mid: Optional[QPointF] = None
        self._update_pen(False)
        self.setToolTip(self.describe())
        self.setZValue(1)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.refresh()

    def describe(self) -> str:
        """A full sentence for the tooltip: what fires what, and how."""
        conn = self.conn_obj
        if conn is None:
            return ""
        line = "%s.%s  →  %s.%s" % (
            self.src_pin.node.entity_name, self.src_pin.pin_name,
            self.dst_pin.node.entity_name, self.dst_pin.pin_name)
        detail = self.label_text()
        if detail:
            line += "\n" + detail
        if not getattr(conn, 'target_id', ''):
            line += "\naddressed by name (a rename will break this wire)"
        return line

    def _update_pen(self, selected: bool):
        """Colour the wire by state: selected, name-addressed, or ordinary.

        A wire with no ``target_id`` resolves by name, which is Fio's legacy
        fallback and breaks the moment its target is renamed. Drawing it dashed
        makes that visible without saying anything is wrong, because it is not —
        it is simply less durable than the rest.
        """
        col = C_CONN_SEL if selected else C_CONN
        by_name = self.conn_obj is not None and not getattr(
            self.conn_obj, 'target_id', '')
        style = Qt.DashLine if by_name else Qt.SolidLine
        self.setPen(QPen(col, 2.2, style, Qt.RoundCap))

    def refresh(self):
        if self.src_pin and self.dst_pin:
            self.prepareGeometryChange()
            self._bezier(self.src_pin.scene_center(),
                         self.dst_pin.scene_center())

    def _bezier(self, p1: QPointF, p2: QPointF):
        path = QPainterPath(p1)
        cx = max(80, abs(p2.x() - p1.x()) * 0.55)
        path.cubicTo(p1 + QPointF(cx, 0),
                     p2 - QPointF(cx, 0), p2)
        self.setPath(path)
        self._mid = path.pointAtPercent(0.5)

    def label_text(self) -> str:
        """What this wire carries, as it reads on the graph.

        A parameter, a delay and a one-shot flag are the whole behaviour of a
        connection, and they used to be invisible — you had to right-click each
        wire in turn to find out what any of them did.
        """
        conn = self.conn_obj
        if conn is None:
            return ""
        parts = []
        if getattr(conn, 'parameter', ''):
            parts.append(str(conn.parameter))
        delay = float(getattr(conn, 'delay', 0.0) or 0.0)
        if delay > 0:
            parts.append("%gs" % delay)
        if getattr(conn, 'fire_once', False):
            parts.append("once")
        return "  ·  ".join(parts)

    def boundingRect(self) -> QRectF:
        """The path, widened to hold the label.

        A nearly straight wire has an almost flat bounding rect, so the label
        box drawn at its midpoint fell outside it and Qt clipped the text away
        entirely — the label existed and was simply never visible.
        """
        rect = super().boundingRect()
        if self._mid is not None and self.label_text():
            rect = rect.united(QRectF(self._mid.x() - 140, self._mid.y() - 16,
                                      280, 32))
        return rect.adjusted(-2, -2, 2, 2)

    def shape(self):
        """Hit-testing stays the wire itself, not the label's box."""
        stroker = QPainterPathStroker()
        stroker.setWidth(8.0)
        return stroker.createStroke(self.path())

    def paint(self, p: QPainter, opt, widget=None):
        super().paint(p, opt, widget)
        text = self.label_text()
        if not text or self._mid is None:
            return
        scene = self.scene()
        if scene is not None and not getattr(scene, 'show_wire_labels', True):
            return

        p.setFont(_ui_font(delta=-2))
        metrics = QFontMetrics(p.font())
        if len(text) > 42:
            text = metrics.elidedText(text, Qt.ElideRight, metrics.averageCharWidth() * 42)
        w = metrics.horizontalAdvance(text) + 10
        h = metrics.height() + 4
        box = QRectF(self._mid.x() - w / 2, self._mid.y() - h / 2, w, h)

        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(20, 20, 24, 215)))
        p.drawRoundedRect(box, 3, 3)
        p.setPen(QPen(QColor(255, 210, 140)))
        p.drawText(box, Qt.AlignCenter, text)

    def hoverEnterEvent(self, ev):
        self.setPen(QPen(C_CONN_SEL, 3.0))
        super().hoverEnterEvent(ev)

    def hoverLeaveEvent(self, ev):
        self._update_pen(self.isSelected())
        super().hoverLeaveEvent(ev)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemSelectedChange:
            self._update_pen(bool(value))
        return super().itemChange(change, value)

    def contextMenuEvent(self, ev):
        menu = QMenu()
        del_act  = menu.addAction("🗑  Delete connection")
        edit_act = menu.addAction("✏  Edit delay / parameter…")
        chosen = menu.exec_(ev.screenPos())
        scene = self.scene()
        if not scene:
            return
        if chosen == del_act:
            scene.remove_connection(self)
        elif chosen == edit_act:
            scene.edit_connection(self)


class DragLine(QGraphicsPathItem):
    def __init__(self, start: QPointF):
        super().__init__()
        self._start = start
        self.setPen(QPen(C_DRAG, 2.0, Qt.DashLine))
        self.setZValue(20)
        self.move_to(start)

    def move_to(self, end: QPointF):
        path = QPainterPath(self._start)
        cx = max(60, abs(end.x() - self._start.x()) * 0.5)
        path.cubicTo(self._start + QPointF(cx, 0),
                     end          - QPointF(cx, 0), end)
        self.setPath(path)


# ── Node ──────────────────────────────────────────────────────────────────────

class EntityNodeItem(QGraphicsItem):
    def __init__(self, entity, entity_type: str, entity_name: str):
        super().__init__()
        self.entity      = entity
        self.entity_type = entity_type
        self.entity_name = entity_name

        self.setFlag(QGraphicsItem.ItemIsMovable,            True)
        self.setFlag(QGraphicsItem.ItemIsSelectable,         True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setZValue(3)

        self._hdr_col = TYPE_HDR.get(entity_type, C_HDR_DEFAULT)

        m = _node_metrics()
        self._pin_r  = m['pin_r']
        self._nw     = m['nw']
        self._nh_hdr = m['nh_hdr']
        self._nh_pin = m['nh_pin']
        self._nh_pad = m['nh_pad']

        self.all_outputs = get_output_names(entity_type) if IO_AVAILABLE else []
        self.all_inputs  = get_input_names(entity_type)  if IO_AVAILABLE else []

        self.out_pins: Dict[str, PinItem] = {}
        self.in_pins:  Dict[str, PinItem] = {}
        for i, name in enumerate(self.all_outputs):
            self.out_pins[name] = PinItem(self, name, True,  i)
        for i, name in enumerate(self.all_inputs):
            self.in_pins[name]  = PinItem(self, name, False, i)

        #: Show every declared pin, or only the ones in use. Entities declare a
        #: lot of I/O — a LogicState has 23 inputs — and a node that drew all of
        #: it at all times would be most of a screen tall for one entity, with
        #: the two pins the map actually uses lost in it.
        self.show_all_pins = False
        #: Pins forced visible while a wire is being dragged, so a drop target
        #: can be seen even when it is collapsed.
        self._revealed = False
        self._hidden_out = 0
        self._hidden_in = 0
        self._rows_out: List[str] = []
        self._rows_in: List[str] = []
        self._h = self._nh_hdr + self._nh_pin + self._nh_pad
        self.relayout()

    # -- pin layout ---------------------------------------------------------

    def connected_pins(self) -> Tuple[set, set]:
        """``(output names, input names)`` that currently carry a wire."""
        scene = self.scene()
        outs, ins = set(), set()
        if scene is None:
            return outs, ins
        for ci in getattr(scene, '_connections', ()):
            if ci.src_pin.node is self:
                outs.add(ci.src_pin.pin_name)
            if ci.dst_pin.node is self:
                ins.add(ci.dst_pin.pin_name)
        return outs, ins

    def relayout(self):
        """Decide which pins are shown, place them, and resize the node.

        Connected pins are always shown — hiding a pin that has a wire on it
        would hide the wire's endpoint. Everything else appears only when the
        node is expanded, or while a drag is looking for somewhere to land.
        """
        if self.show_all_pins or self._revealed:
            visible_out = list(self.all_outputs)
            visible_in = list(self.all_inputs)
        else:
            used_out, used_in = self.connected_pins()
            visible_out = [n for n in self.all_outputs if n in used_out]
            visible_in = [n for n in self.all_inputs if n in used_in]

        self._hidden_out = len(self.all_outputs) - len(visible_out)
        self._hidden_in = len(self.all_inputs) - len(visible_in)
        self._rows_out, self._rows_in = visible_out, visible_in

        for name, pin in self.out_pins.items():
            pin.place(visible_out.index(name) if name in visible_out else None)
        for name, pin in self.in_pins.items():
            pin.place(visible_in.index(name) if name in visible_in else None)

        rows = max(len(visible_out), len(visible_in), 1)
        if self._hidden_out or self._hidden_in:
            rows += 1                      # the "N more…" row
        new_h = self._nh_hdr + rows * self._nh_pin + self._nh_pad
        if new_h != self._h:
            self.prepareGeometryChange()
            self._h = new_h
        self.update()
        scene = self.scene()
        if scene is not None:
            scene.refresh_node_connections(self)

    def set_show_all_pins(self, show: bool):
        """Expand or collapse this node's pin list."""
        if bool(show) == self.show_all_pins:
            return
        self.show_all_pins = bool(show)
        self.relayout()
        # A node that just grew by thirty rows will be sitting on top of
        # whatever was below it, so the auto-placed column is repacked. Nodes
        # the user has positioned are left alone (see reflow_auto_placed).
        scene = self.scene()
        if scene is not None and hasattr(scene, 'reflow_auto_placed'):
            scene.reflow_auto_placed()

    def reveal_for_drag(self, revealed: bool):
        """Temporarily show every pin while a wire is being dragged."""
        if bool(revealed) == self._revealed:
            return
        self._revealed = bool(revealed)
        self.relayout()

    def boundingRect(self) -> QRectF:
        return QRectF(-2, -2, self._nw + 4, self._h + 4)

    def paint(self, p: QPainter, opt, widget=None):
        sel  = self.isSelected()
        bg   = C_BODY_SEL   if sel else C_BODY
        bord = C_BORDER_SEL if sel else C_BORDER
        r    = 7.0
        nw   = self._nw
        nh   = self._h
        hh   = self._nh_hdr
        pr   = self._pin_r
        row  = self._nh_pin

        p.setBrush(QBrush(QColor(0, 0, 0, 50)))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(QRectF(4, 4, nw, nh), r, r)

        p.setBrush(QBrush(bg))
        p.setPen(QPen(bord, 1.5))
        p.drawRoundedRect(QRectF(0, 0, nw, nh), r, r)

        hdr  = QRectF(0, 0, nw, hh)
        grad = QLinearGradient(hdr.topLeft(), hdr.bottomLeft())
        grad.setColorAt(0, self._hdr_col.lighter(140))
        grad.setColorAt(1, self._hdr_col)
        p.setBrush(QBrush(grad))
        p.setPen(Qt.NoPen)
        p.drawRoundedRect(hdr, r, r)
        p.drawRect(QRectF(0, hh - r, nw, r))

        # The type is drawn first and its width reserved, so a long entity name
        # is elided rather than printed straight through it. Both used to be
        # drawn across the full header width — one left-aligned, one right — and
        # a name like "LevelChanger_1" landed on top of "levelchanger".
        # The name is what a designer addresses the entity by, so it gets the
        # room it needs and the type is elided into what is left. The other way
        # round turned "LevelChanger_1" into "Lev...r_1" to protect a label that
        # merely repeats the node's colour.
        # Measured through the painter's own metrics, not a detached
        # QFontMetrics: those disagree by a pixel or two depending on the paint
        # device, and a name measured one pixel narrow than it draws gets
        # elided when it would have fitted exactly.
        p.setFont(_ui_font(bold=True))
        name_metrics = p.fontMetrics()
        name_w = min(float(name_metrics.horizontalAdvance(self.entity_name)) + 2.0,
                     nw - 24)
        name_text = name_metrics.elidedText(self.entity_name, Qt.ElideMiddle,
                                            int(name_w) + 1)
        p.setPen(QPen(Qt.white))
        p.drawText(QRectF(10, 0, name_w, hh), Qt.AlignVCenter | Qt.AlignLeft,
                   name_text)

        type_w = max(0.0, nw - name_w - 24)
        if type_w > 12:
            p.setFont(_ui_font(delta=-2))
            type_metrics = p.fontMetrics()
            p.setPen(QPen(QColor(255, 255, 255, 130)))
            p.drawText(QRectF(nw - type_w - 8, 0, type_w, hh),
                       Qt.AlignVCenter | Qt.AlignRight,
                       type_metrics.elidedText(self.entity_type, Qt.ElideRight,
                                               int(type_w)))

        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        p.drawLine(QPointF(0, hh), QPointF(nw, hh))

        p.setFont(_ui_font(delta=-1))
        for i, name in enumerate(self._rows_out):
            y = hh + i * row
            p.setPen(QPen(C_OUT.lighter(140)))
            p.drawText(QRectF(0, y, nw - pr * 2 - 6, row),
                       Qt.AlignVCenter | Qt.AlignRight, name)

        for i, name in enumerate(self._rows_in):
            y = hh + i * row
            p.setPen(QPen(C_IN.lighter(140)))
            p.drawText(QRectF(pr * 2 + 6, y, nw - pr * 2 - 12, row),
                       Qt.AlignVCenter | Qt.AlignLeft, name)

        # The collapsed remainder, so a node never silently omits anything: it
        # says how much it is not showing, and double-click opens it.
        if self._hidden_out or self._hidden_in:
            # Each side gets half the row and is elided into it. Both notices
            # used to be drawn across nearly the whole node width — one aligned
            # left, one right — so on any node with both they collided in the
            # middle and rendered as unreadable overstruck text.
            y = hh + max(len(self._rows_out), len(self._rows_in)) * row
            more_font = _ui_font(delta=-2)
            more_metrics = QFontMetrics(more_font)
            p.setFont(more_font)
            p.setPen(QPen(QColor(200, 200, 210, 150)))
            half = (nw - pr * 2 - 12) / 2.0
            if self._hidden_in:
                text = more_metrics.elidedText("+%d in" % self._hidden_in,
                                               Qt.ElideRight, int(half))
                p.drawText(QRectF(pr * 2 + 6, y, half, row),
                           Qt.AlignVCenter | Qt.AlignLeft, text)
            if self._hidden_out:
                text = more_metrics.elidedText("%d out+" % self._hidden_out,
                                               Qt.ElideRight, int(half))
                p.drawText(QRectF(nw - pr * 2 - 6 - half, y, half, row),
                           Qt.AlignVCenter | Qt.AlignRight, text)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            scene = self.scene()
            if scene:
                scene.refresh_node_connections(self)
        return super().itemChange(change, value)

    def mouseDoubleClickEvent(self, ev):
        """Expand or collapse the pin list — the fastest way to reach a pin."""
        self.set_show_all_pins(not self.show_all_pins)
        ev.accept()

    def contextMenuEvent(self, ev):
        menu = QMenu()
        lbl = menu.addAction(f"{self.entity_name}  [{self.entity_type}]")
        lbl.setEnabled(False)
        menu.addSeparator()

        hidden = self._hidden_in + self._hidden_out
        if self.show_all_pins:
            menu.addAction("▾  Show only connected pins",
                           lambda: self.set_show_all_pins(False))
        else:
            menu.addAction("▸  Show all %d pins" % (len(self.all_inputs)
                                                    + len(self.all_outputs)),
                           lambda: self.set_show_all_pins(True))
        if hidden and not self.show_all_pins:
            menu.addAction("(%d pin%s hidden — double-click the node to expand)"
                           % (hidden, "" if hidden == 1 else "s")).setEnabled(False)

        menu.addSeparator()
        scene = self.scene()
        menu.addAction(
            "Clear all connections from this node",
            lambda: scene.clear_node_connections(self) if scene else None
        )
        if scene is not None:
            menu.addAction("Select in editor",
                           lambda: scene.request_select(self.entity))
        menu.exec_(ev.screenPos())


# ── Scene ─────────────────────────────────────────────────────────────────────

class LogicGraphScene(QGraphicsScene):
    connections_changed = pyqtSignal()
    #: Asks the host to select an entity in the editor's other views.
    select_entity = pyqtSignal(object)

    def __init__(self, editor_state, parent=None):
        super().__init__(parent)
        self.editor_state = editor_state
        self.setBackgroundBrush(QBrush(QColor(28, 28, 32)))

        self._nodes:       Dict[str, EntityNodeItem] = {}
        self._connections: List[ConnectionItem]      = []
        self._drag_line:   Optional[DragLine]        = None
        self._drag_src:    Optional[PinItem]         = None
        #: Whether wires draw their parameter/delay/once label.
        self.show_wire_labels = True

        self._undrawable: Dict[int, Tuple[object, List]] = {}
        self._build_nodes()
        self._build_connections()
        self._fix_source_target_order()
        # Pin visibility depends on which pins ended up with wires, so it can
        # only be decided once the wires exist — and a node's height follows
        # from that, so the grid can only be packed afterwards.
        for node in self._nodes.values():
            node.relayout()
        self.reflow_auto_placed()

    def reflow_auto_placed(self):
        """Pack the auto-placed nodes into columns using their real heights.

        The first layout put nodes on a grid with one fixed row height, guessed
        from a nine-pin node. Node heights vary by an order of magnitude now — a
        collapsed node is one row, an expanded LogicState is thirty-four — so a
        fixed step either overlapped the row below or left a screen of space
        between rows. This walks each column with a cursor instead.

        Nodes the user has placed are left exactly where they are: a saved
        position is a decision, and re-packing it would undo that decision every
        time the window opened.
        """
        saved = getattr(self.editor_state, '_logic_graph_positions', {}) or {}
        auto = [(key, node) for key, node in self._nodes.items() if key not in saved]
        if not auto:
            return

        metrics = _node_metrics()
        spacing_x = metrics['nw'] + 60
        per_column = max(1, int(len(auto) ** 0.5))

        column, cursor = 0, 0.0
        placed_in_column = 0
        for _key, node in auto:
            node.setPos(column * spacing_x, cursor)
            cursor += node._h + 40
            placed_in_column += 1
            if placed_in_column >= per_column:
                column += 1
                cursor = 0.0
                placed_in_column = 0

        for ci in self._connections:
            ci.refresh()

    def request_select(self, entity):
        self.select_entity.emit(entity)

    def set_wire_labels(self, shown: bool):
        self.show_wire_labels = bool(shown)
        for ci in self._connections:
            ci.update()

    def set_all_pins_expanded(self, expanded: bool):
        for node in self._nodes.values():
            if bool(expanded) != node.show_all_pins:
                node.show_all_pins = bool(expanded)
                node.relayout()
        self.reflow_auto_placed()

    def find_nodes(self, text: str) -> List[EntityNodeItem]:
        """Nodes whose name or type contains *text*, case-insensitively."""
        needle = (text or "").strip().lower()
        if not needle:
            return []
        return [n for n in self._nodes.values()
                if needle in n.entity_name.lower() or needle in n.entity_type.lower()]

    def highlight(self, nodes):
        """Select *nodes* and nothing else, so the view can frame them."""
        self.clearSelection()
        for node in nodes:
            node.setSelected(True)

    def auto_arrange(self):
        """Lay the graph out left-to-right by how far downstream each node is.

        A node's column is the longest chain of connections that reaches it, so
        a source sits left of everything it drives and a chain reads in the
        order it fires. Cycles are common and legitimate in Fio logic (a relay
        that re-arms itself), so the walk is depth-limited rather than assuming
        a DAG.
        """
        nodes = list(self._nodes.values())
        if not nodes:
            return
        downstream: Dict[int, List[EntityNodeItem]] = {}
        indegree = {id(n): 0 for n in nodes}
        for ci in self._connections:
            a, b = ci.src_pin.node, ci.dst_pin.node
            if a is b:
                continue
            downstream.setdefault(id(a), []).append(b)
            indegree[id(b)] = indegree.get(id(b), 0) + 1

        column = {id(n): 0 for n in nodes}
        frontier = [n for n in nodes if indegree.get(id(n), 0) == 0] or nodes
        seen_depth = 0
        while frontier and seen_depth < len(nodes) + 1:
            nxt = []
            for node in frontier:
                for target in downstream.get(id(node), ()):
                    if column[id(target)] < column[id(node)] + 1:
                        column[id(target)] = column[id(node)] + 1
                        nxt.append(target)
            frontier = nxt
            seen_depth += 1

        metrics = _node_metrics()
        spacing_x = metrics['nw'] + 90
        by_column: Dict[int, List[EntityNodeItem]] = {}
        for node in nodes:
            by_column.setdefault(column[id(node)], []).append(node)

        for col in sorted(by_column):
            y = 0.0
            for node in sorted(by_column[col], key=lambda n: n.entity_name.lower()):
                node.setPos(col * spacing_x, y)
                y += node._h + 40
        for ci in self._connections:
            ci.refresh()
        self.store_positions()

    def undrawable_report(self) -> List[str]:
        """One line per connection the graph cannot show, and why."""
        lines = []
        for _key, (entity, conns) in self._undrawable.items():
            src = self._entity_name(entity) or '<unnamed>'
            for conn in conns:
                if not hasattr(conn, 'output_name'):
                    lines.append("%s: a connection in an old raw format" % src)
                    continue
                lines.append("%s.%s → %s.%s" % (
                    src, conn.output_name, conn.target_name or '<none>',
                    conn.input_name))
        return sorted(lines)

    def _entity_type(self, entity) -> str:
        if IO_AVAILABLE:
            return get_entity_type_for_io(entity)
        return (entity.properties.get('type', 'unknown')
                if hasattr(entity, 'properties') else 'brush')

    def _entity_name(self, entity) -> str:
        if hasattr(entity, 'properties'):
            return entity.properties.get('name', '?')
        return entity.get('name', '?')

    def _entity_connections(self, entity):
        if hasattr(entity, 'properties'):
            return entity.properties.get('_io_connections', [])
        return entity.get('_io_connections', [])

    def _entity_id(self, entity) -> str:
        """Return the stable UUID of an entity."""
        if hasattr(entity, 'properties'):
            return entity.properties.get('id', '')
        return entity.get('id', '')

    def _build_nodes(self):
        col, row, per_row = 0, 0, 4
        m         = _node_metrics()
        spacing_x = m['nw'] + 60
        spacing_y = m['nh_hdr'] + 9 * m['nh_pin'] + 60

        all_entities = []
        for t in self.editor_state.things:
            name = self._entity_name(t)
            if name and name != '?':
                all_entities.append(t)
        for b in self.editor_state.brushes:
            name = b.get('name', '')
            if name:
                all_entities.append(b)

        for entity in all_entities:
            etype = self._entity_type(entity)
            ename = self._entity_name(entity)
            eid   = self._entity_id(entity)
            node  = EntityNodeItem(entity, etype, ename)

            # Restore saved position from map file, or fall back to grid layout
            saved_positions = getattr(self.editor_state, '_logic_graph_positions', {})
            if eid and eid in saved_positions:
                pos = saved_positions[eid]
                node.setPos(pos['x'], pos['y'])
            else:
                node.setPos(col * spacing_x, row * spacing_y)
                col += 1
                if col >= per_row:
                    col = 0
                    row += 1

            self.addItem(node)
            # Key by stable entity ID; fall back to Python id for entities without one
            self._nodes[eid if eid else str(id(entity))] = node

    def _build_connections(self):
        """Draw a wire for every connection the graph can represent.

        Whatever cannot be drawn — a target that is not in the map, an output or
        input name no node has a pin for — is *remembered* rather than dropped,
        in :attr:`_undrawable`. :meth:`apply_to_entities` writes those back
        untouched, because a connection the graph cannot draw is still a
        connection the map has, and Apply used to delete every one of them.
        """
        name_to_node: Dict[str, EntityNodeItem] = {}
        for node in self._nodes.values():
            # First wins, so a duplicate name cannot silently reassign wires
            # that the id lookup would have resolved correctly.
            name_to_node.setdefault(node.entity_name, node)
        id_to_node: Dict[str, EntityNodeItem] = {
            eid: node for eid, node in self._nodes.items() if eid
        }

        #: entity -> [connections this graph could not draw], kept so Apply can
        #: put them back exactly as they were.
        self._undrawable: Dict[int, Tuple[object, List]] = {}

        def _keep(entity, conn):
            self._undrawable.setdefault(id(entity), (entity, []))[1].append(conn)

        all_entities = list(self.editor_state.things) + list(self.editor_state.brushes)
        for entity in all_entities:
            src_node = self._node_for(entity)
            for conn in self._entity_connections(entity):
                if not hasattr(conn, 'output_name'):
                    _keep(entity, conn)          # a raw dict from an old map
                    continue
                if src_node is None:
                    _keep(entity, conn)          # unnamed entity: it has no node
                    continue
                # Prefer ID lookup, fall back to name
                dst_node = None
                target_id = getattr(conn, 'target_id', '')
                if target_id:
                    dst_node = id_to_node.get(target_id)
                if dst_node is None:
                    dst_node = name_to_node.get(conn.target_name)
                src_pin = src_node.out_pins.get(conn.output_name) if dst_node else None
                dst_pin = dst_node.in_pins.get(conn.input_name) if dst_node else None
                if src_pin and dst_pin:
                    ci = ConnectionItem(src_pin, dst_pin, conn)
                    self.addItem(ci)
                    self._connections.append(ci)
                else:
                    # A missing target, or a pin name the entity type does not
                    # declare (a typo, or a map from a newer Fio). Undrawable,
                    # not unwanted.
                    _keep(entity, conn)

    def _node_for(self, entity) -> Optional[EntityNodeItem]:
        """The node representing *entity*, preferring its UUID.

        Name lookup is the fallback and not the key: two entities may share a
        name, and a graph that keyed on names would quietly wire one of them to
        the other's connections.
        """
        eid = self._entity_id(entity)
        if eid:
            node = self._nodes.get(eid)
            if node is not None:
                return node
        node = self._nodes.get(str(id(entity)))
        if node is not None:
            return node
        name = self._entity_name(entity)
        for candidate in self._nodes.values():
            if candidate.entity is entity or candidate.entity_name == name:
                return candidate
        return None

    def undrawable_count(self) -> int:
        """How many connections exist that the graph cannot show."""
        return sum(len(conns) for _entity, conns in self._undrawable.values())

    def store_positions(self):
        """Publish node positions to the editor state, so a save keeps them.

        The graph pushes rather than the save pulling: the save has no way to
        find this window (it belongs to the main window, not the state), and a
        pushed value is also correct once this window has closed.
        """
        positions = {}
        for key, node in self._nodes.items():
            if key:
                positions[key] = {'x': float(node.x()), 'y': float(node.y())}
        try:
            self.editor_state._logic_graph_positions = positions
        except Exception:                                # pragma: no cover
            pass
        return positions

    def _fix_source_target_order(self):
        """Swap node positions so source nodes sit left of their targets.

        Only affects nodes that were auto-placed by the grid layout
        (i.e. those without saved positions).  Nodes with persisted
        positions are left alone.
        """
        saved = getattr(self.editor_state, '_logic_graph_positions', {})

        # Collect (src_node, dst_node) pairs that need checking
        swapped = set()
        for ci in self._connections:
            src_node = ci.src_pin.node
            dst_node = ci.dst_pin.node
            if src_node is dst_node:
                continue

            src_id = self._entity_id(src_node.entity)
            dst_id = self._entity_id(dst_node.entity)

            # Skip if either has a saved position — the user placed them deliberately
            if (src_id and src_id in saved) or (dst_id and dst_id in saved):
                continue

            # If source is to the right of (or on top of) the target, swap
            pair_key = (id(src_node), id(dst_node))
            if pair_key in swapped:
                continue

            if src_node.x() >= dst_node.x():
                sx, sy = src_node.x(), src_node.y()
                dx, dy = dst_node.x(), dst_node.y()
                src_node.setPos(dx, dy)
                dst_node.setPos(sx, sy)
                swapped.add(pair_key)
                swapped.add((id(dst_node), id(src_node)))

        # Refresh all connection lines after moves
        if swapped:
            for ci in self._connections:
                ci.refresh()

    def refresh_node_connections(self, node: EntityNodeItem):
        for ci in self._connections:
            if ci.src_pin.node is node or ci.dst_pin.node is node:
                ci.refresh()

    def _pin_at(self, scene_pos: QPointF) -> Optional[PinItem]:
        for item in self.items(scene_pos):
            if isinstance(item, PinItem):
                return item
        return None

    def mousePressEvent(self, ev):
        if ev.button() == Qt.LeftButton:
            pin = self._pin_at(ev.scenePos())
            if pin and pin.is_output:
                self._drag_src  = pin
                self._drag_line = DragLine(pin.scene_center())
                self.addItem(self._drag_line)
                self._reveal_drop_targets(True)
                ev.accept()
                return
        super().mousePressEvent(ev)

    def _reveal_drop_targets(self, revealed: bool):
        """Open every other node's inputs while a wire is being dragged.

        A collapsed node shows only the pins already in use, which is what makes
        a large entity readable — and would make it impossible to drop a *new*
        wire on. Revealing for the duration of the drag keeps both.
        """
        source_node = self._drag_src.node if self._drag_src else None
        for node in self._nodes.values():
            if node is not source_node:
                node.reveal_for_drag(revealed)

    def mouseMoveEvent(self, ev):
        if self._drag_line and self._drag_src:
            self._drag_line.move_to(ev.scenePos())
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._drag_line and self._drag_src:
            source = self._drag_src
            self.removeItem(self._drag_line)
            self._drag_line = None
            self._drag_src  = None
            self._reveal_drop_targets(False)

            dst = self._pin_at(ev.scenePos())
            if dst and not dst.is_output and dst.node is not source.node:
                self._create_connection(source, dst)
            else:
                # Dropped on a node but not on a pin: offer that node's inputs,
                # which is faster than hunting for the right row and is how the
                # same gesture behaves in a Blueprint graph.
                node = self._node_at(ev.scenePos())
                if node is not None and node is not source.node:
                    self._quick_connect(source, node, ev.screenPos())
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def _node_at(self, scene_pos: QPointF) -> Optional[EntityNodeItem]:
        for item in self.items(scene_pos):
            if isinstance(item, EntityNodeItem):
                return item
            if isinstance(item, PinItem):
                return item.node
        return None

    def _quick_connect(self, source: PinItem, node: EntityNodeItem, screen_pos):
        """Pick an input on *node* from a menu, and wire *source* to it."""
        if not node.all_inputs:
            return
        menu = QMenu()
        header = menu.addAction("%s  →  %s" % (source.pin_name, node.entity_name))
        header.setEnabled(False)
        menu.addSeparator()

        used = {ci.dst_pin.pin_name for ci in self._connections
                if ci.dst_pin.node is node}
        descriptions = {}
        if IO_AVAILABLE:
            for io_def in IO_REGISTRY.get(node.entity_type, {}).get('inputs', []):
                descriptions[io_def.name] = io_def.description

        actions = {}
        for name in node.all_inputs:
            label = ("● " if name in used else "   ") + name
            act = menu.addAction(label)
            if descriptions.get(name):
                act.setToolTip(descriptions[name])
            actions[act] = name

        chosen = menu.exec_(screen_pos)
        target = actions.get(chosen)
        if target:
            dst = node.in_pins.get(target)
            if dst is not None:
                self._create_connection(source, dst)

    def keyPressEvent(self, ev):
        if ev.key() == Qt.Key_Delete:
            for item in self.selectedItems():
                if isinstance(item, ConnectionItem):
                    self.remove_connection(item)
        super().keyPressEvent(ev)

    def _create_connection(self, src: PinItem, dst: PinItem,
                           param: str = "", delay: float = 0.0,
                           fire_once: bool = False):
        if not IO_AVAILABLE or OutputConnection is None:
            return
        for ci in self._connections:
            if ci.src_pin is src and ci.dst_pin is dst:
                return
        conn = OutputConnection(
            output_name = src.pin_name,
            target_name = dst.node.entity_name,
            input_name  = dst.pin_name,
            parameter   = param,
            delay       = delay,
            fire_once   = fire_once,
            target_id   = self._entity_id(dst.node.entity),
        )
        ci = ConnectionItem(src, dst, conn)
        self.addItem(ci)
        self._connections.append(ci)
        src.node.relayout()
        dst.node.relayout()
        self.connections_changed.emit()

    def remove_connection(self, ci: ConnectionItem):
        if ci in self._connections:
            self._connections.remove(ci)
        self.removeItem(ci)
        # A pin that was only shown because it had a wire can collapse again.
        ci.src_pin.node.relayout()
        ci.dst_pin.node.relayout()
        self.connections_changed.emit()

    def clear_node_connections(self, node: EntityNodeItem):
        to_remove = [ci for ci in self._connections
                     if ci.src_pin.node is node or ci.dst_pin.node is node]
        for ci in to_remove:
            self.remove_connection(ci)

    def edit_connection(self, ci: ConnectionItem):
        dlg = _ConnectionEditDialog(ci.conn_obj, parent=None)
        if dlg.exec_() == QDialog.Accepted and ci.conn_obj:
            ci.conn_obj.delay     = dlg.delay()
            ci.conn_obj.parameter = dlg.parameter()
            ci.conn_obj.fire_once = dlg.fire_once()
            self.connections_changed.emit()

    def apply_to_entities(self) -> int:
        """Write the graph back to the scene. Returns how many wires were written.

        Only entities the graph actually represents are rewritten. That matters
        more than it sounds: this used to clear ``_io_connections`` on *every*
        entity in the scene and then write back only what it had drawn, so
        pressing Apply destroyed every connection the graph could not show — one
        naming a target that had been deleted, one with a typo in an input name,
        and every connection on an unnamed entity, since only named entities get
        a node. Opening the window and pressing Apply was enough to lose them,
        with nothing said.

        Undrawable connections are restored alongside the drawn ones, in their
        original order relative to each other, so a round trip through this
        window is a no-op for anything it did not touch.
        """
        if not IO_AVAILABLE:
            return 0

        drawn: Dict[int, List] = {}
        touched: Dict[int, object] = {}
        for node in self._nodes.values():
            touched[id(node.entity)] = node.entity
            drawn[id(node.entity)] = []

        count = 0
        for ci in self._connections:
            if ci.conn_obj is None:
                continue
            src_entity = ci.src_pin.node.entity
            drawn.setdefault(id(src_entity), []).append(ci.conn_obj)
            touched[id(src_entity)] = src_entity
            count += 1

        # Entities that only carry connections the graph could not draw are
        # rewritten too — with exactly what they had.
        for key, (entity, conns) in self._undrawable.items():
            touched.setdefault(key, entity)
            drawn.setdefault(key, [])

        for key, entity in touched.items():
            connections = list(drawn.get(key, ()))
            connections.extend(self._undrawable.get(key, (None, ()))[1])
            if hasattr(entity, 'properties'):
                entity.properties['_io_connections'] = connections
            else:
                entity['_io_connections'] = connections

        # The reverse-target index is cached against a revision counter, so
        # anything asking "what points at this entity?" — the Property Editor's
        # "Targeted by" list above all — keeps answering from the pre-Apply
        # graph until the counter moves.
        try:
            from .io_system import bump_io_revision
        except ImportError:                              # pragma: no cover
            from editor.io_system import bump_io_revision
        bump_io_revision()
        self.store_positions()
        return count

    def add_connection_by_name(self, src_name: str, out_pin: str,
                               dst_name: str, in_pin: str,
                               param: str = "", delay: float = 0.0,
                               fire_once: bool = False) -> bool:
        name_map = {n.entity_name: n for n in self._nodes.values()}
        src_node = name_map.get(src_name)
        dst_node = name_map.get(dst_name)
        if not src_node or not dst_node:
            return False
        src_pin = src_node.out_pins.get(out_pin)
        dst_pin = dst_node.in_pins.get(in_pin)
        if not src_pin or not dst_pin:
            return False
        self._create_connection(src_pin, dst_pin, param, delay, fire_once)
        return True


# ── Connection edit dialog ────────────────────────────────────────────────────

class _ConnectionEditDialog(QDialog):
    def __init__(self, conn: Optional[OutputConnection], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Edit Connection")
        self.setMinimumWidth(340)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self._param = QLineEdit(conn.parameter if conn else "")
        self._param.setPlaceholderText("Optional value passed to input")
        form.addRow("Parameter:", self._param)
        self._delay = QDoubleSpinBox()
        self._delay.setRange(0.0, 999.0)
        self._delay.setSingleStep(0.1)
        self._delay.setDecimals(2)
        self._delay.setSuffix(" sec")
        self._delay.setValue(conn.delay if conn else 0.0)
        form.addRow("Delay:", self._delay)
        self._once = QCheckBox("Fire once per play session")
        self._once.setChecked(conn.fire_once if conn else False)
        form.addRow("", self._once)
        layout.addLayout(form)
        btns = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        layout.addWidget(btns)

    def parameter(self) -> str:   return self._param.text().strip()
    def delay(self)     -> float: return self._delay.value()
    def fire_once(self) -> bool:  return self._once.isChecked()


# ── View ──────────────────────────────────────────────────────────────────────

class LogicGraphView(QGraphicsView):
    """QGraphicsView with middle-mouse pan, scroll-wheel zoom, and node-size controls."""

    def __init__(self, scene: LogicGraphScene, parent=None):
        super().__init__(scene, parent)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setDragMode(QGraphicsView.RubberBandDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setFrameShape(QFrame.NoFrame)
        self._panning   = False
        self._pan_start = None

        # Floating node size controls
        self.btn_container = QWidget(self)
        # Fix the width explicitly so the right-align math works immediately
        self.btn_container.setFixedWidth(72) 
        btn_lay = QHBoxLayout(self.btn_container)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(4)

        self.btn_plus = QPushButton("+", self.btn_container)
        self.btn_minus = QPushButton("-", self.btn_container)
        
        for btn in (self.btn_plus, self.btn_minus):
            btn.setFixedSize(32, 32)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setStyleSheet("""
                QPushButton {
                    background: #333338;
                    border: 1px solid #555;
                    border-radius: 4px;
                    font-size: 18px;
                    font-weight: bold;
                    color: white;
                }
                QPushButton:hover { background: #44444a; }
            """)
            btn_lay.addWidget(btn)

        self.btn_plus.setToolTip("Zoom in (mouse wheel)")
        self.btn_minus.setToolTip("Zoom out (mouse wheel)")
        self.btn_plus.clicked.connect(lambda: self._adjust_node_size(1))
        self.btn_minus.clicked.connect(lambda: self._adjust_node_size(-1))
        
        # Force layout update so width() is known for the first resizeEvent
        self.btn_container.adjustSize()

    def resizeEvent(self, event):
        """Keep zoom buttons in the top right."""
        super().resizeEvent(event)
        # Position using the fixed container width
        self.btn_container.move(self.width() - self.btn_container.width() - 20, 20)

    def _adjust_node_size(self, delta: int):
        """Grow or shrink the nodes.

        Scales the *view*, not the application font. The buttons used to call
        ``QApplication.setFont`` — which resized every window in the editor, not
        the graph — and then reload the whole scene to pick the change up, which
        also discarded any unapplied edits. Zooming the view is what the buttons
        always meant, and it costs nothing.
        """
        factor = 1.15 if delta > 0 else 1.0 / 1.15
        self.scale(factor, factor)

    def wheelEvent(self, ev):
        factor = 1.15 if ev.angleDelta().y() > 0 else 1.0 / 1.15
        self.scale(factor, factor)

    def mousePressEvent(self, ev):
        if ev.button() == Qt.MiddleButton:
            self._panning   = True
            self._pan_start = ev.pos()
            self.setCursor(Qt.ClosedHandCursor)
            ev.accept()
            return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._panning and self._pan_start is not None:
            delta           = ev.pos() - self._pan_start
            self._pan_start = ev.pos()
            self.horizontalScrollBar().setValue(
                self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(
                self.verticalScrollBar().value() - delta.y())
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if ev.button() == Qt.MiddleButton:
            self._panning = False
            self.setCursor(Qt.ArrowCursor)
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

    def fit_all(self):
        self.fitInView(
            self.scene().itemsBoundingRect().adjusted(-40, -40, 40, 40),
            Qt.KeepAspectRatio
        )

    def frame_selected(self):
        """Zoom to the selection, or to everything when nothing is selected."""
        scene = self.scene()
        if scene is None:
            return
        items = [i for i in scene.selectedItems() if isinstance(i, EntityNodeItem)]
        if not items:
            self.fit_all()
            return
        rect = items[0].sceneBoundingRect()
        for item in items[1:]:
            rect = rect.united(item.sceneBoundingRect())
        self.fitInView(rect.adjusted(-80, -80, 80, 80), Qt.KeepAspectRatio)


# ── Window ────────────────────────────────────────────────────────────────────

class LogicGraphWindow(QWidget):
    #: Emitted immediately *before* the graph rewrites the scene, so a host can
    #: checkpoint for undo. Fio's tools checkpoint at the start of a gesture,
    #: never the end, so the entry on the stack is the state to go back to.
    about_to_apply = pyqtSignal()
    applied = pyqtSignal()
    #: Relays a node's "Select in editor" to the host.
    select_entity = pyqtSignal(object)

    def __init__(self, editor_state, parent=None):
        super().__init__(parent, Qt.Window)
        self.editor_state = editor_state
        self._dirty = False
        self._first_show = True
        self.setWindowTitle("Logic Graph Editor")
        self.resize(1200, 750)
        self.setMinimumSize(800, 500)
        self._build_ui()
        self._apply_style()

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        fm        = QFontMetrics(QApplication.font())
        toolbar_h = fm.height() + 26
        tb = QWidget()
        tb.setObjectName("graphToolbar")
        tb.setFixedHeight(toolbar_h)
        tb_lay = QHBoxLayout(tb)
        tb_lay.setContentsMargins(10, 4, 10, 4)
        tb_lay.setSpacing(6)

        self._lbl_status = QLabel(
            "Drag output pins (orange) → input pins (blue) to connect.")
        self._lbl_status.setStyleSheet("color:#aaa;")
        tb_lay.addWidget(self._lbl_status)

        self._search = QLineEdit()
        self._search.setPlaceholderText("Find entity…  (Ctrl+F)")
        self._search.setFixedWidth(220)
        self._search.setClearButtonEnabled(True)
        self._search.textChanged.connect(self._on_search)
        self._search.returnPressed.connect(
            lambda: self.graph_view.frame_selected())
        tb_lay.addWidget(self._search)

        tb_lay.addStretch()

        self._chk_labels = QCheckBox("Wire info")
        self._chk_labels.setChecked(True)
        self._chk_labels.setToolTip(
            "Show each connection's parameter, delay and fire-once flag on the wire")
        self._chk_labels.toggled.connect(
            lambda on: self.graph_scene.set_wire_labels(on))
        tb_lay.addWidget(self._chk_labels)

        self._chk_pins = QCheckBox("All pins")
        self._chk_pins.setChecked(False)
        self._chk_pins.setToolTip(
            "Show every declared input and output, not only the connected ones\n"
            "(double-click a node to expand just that one)")
        self._chk_pins.toggled.connect(
            lambda on: self.graph_scene.set_all_pins_expanded(on))
        tb_lay.addWidget(self._chk_pins)

        btn_arrange = QPushButton("⇄ Arrange")
        btn_arrange.setToolTip("Lay the graph out left-to-right by signal flow")
        btn_arrange.setFixedWidth(120)
        btn_arrange.clicked.connect(self._arrange)
        tb_lay.addWidget(btn_arrange)

        btn_refresh = QPushButton("↺ Reload")
        btn_refresh.setToolTip("Reload nodes from scene")
        btn_refresh.setFixedWidth(150)
        btn_refresh.clicked.connect(self._reload)

        btn_wizard = QPushButton("✨ Wizard…")
        btn_wizard.setToolTip("Open the Logic Wizard for guided setup")
        btn_wizard.setFixedWidth(300)
        btn_wizard.clicked.connect(self._open_wizard)

        self._btn_apply = QPushButton("✔ Apply")
        self._btn_apply.setToolTip(
            "Write graph connections back to entities (Ctrl+S)")
        self._btn_apply.setFixedWidth(300)
        self._btn_apply.setObjectName("applyBtn")
        self._btn_apply.clicked.connect(self._apply)

        btn_refresh.setFixedWidth(110)
        btn_wizard.setFixedWidth(130)
        self._btn_apply.setFixedWidth(120)
        for w in (btn_refresh, btn_wizard, self._btn_apply):
            tb_lay.addWidget(w)

        root.addWidget(tb)

        self.graph_scene = LogicGraphScene(self.editor_state)
        self.graph_scene.connections_changed.connect(self._on_changed)
        self.graph_scene.select_entity.connect(self.select_entity)
        self.graph_view  = LogicGraphView(self.graph_scene)
        root.addWidget(self.graph_view)

        self._status = QLabel("Ready.")
        self._status.setObjectName("statusBar")
        self._status.setFixedHeight(fm.height() + 10)
        self._status.setContentsMargins(10, 0, 10, 0)
        root.addWidget(self._status)

        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._apply)
        QShortcut(QKeySequence("Ctrl+F"), self).activated.connect(
            lambda: (self._search.setFocus(), self._search.selectAll()))
        QShortcut(QKeySequence("F"), self).activated.connect(
            lambda: self.graph_view.frame_selected())
        QShortcut(QKeySequence("Home"), self).activated.connect(
            lambda: self.graph_view.fit_all())
        self._refresh_undrawable_notice()

    def _on_search(self, text: str):
        """Select what matches, so F frames it and the eye can find it."""
        matches = self.graph_scene.find_nodes(text)
        self.graph_scene.highlight(matches)
        if not text.strip():
            self._refresh_undrawable_notice()
        elif matches:
            self._status.setText("%d entit%s matching '%s' — press Enter to frame."
                                 % (len(matches),
                                    "y" if len(matches) == 1 else "ies", text))
        else:
            self._status.setText("No entity matches '%s'." % text)

    def _arrange(self):
        self.graph_scene.auto_arrange()
        self.graph_view.fit_all()
        self._status.setText("Arranged by signal flow.")
        self._on_changed()

    def _refresh_undrawable_notice(self):
        """Say plainly when the map holds connections the graph cannot show.

        They are preserved through Apply either way, but a graph that quietly
        showed less than the map holds would be lying by omission.
        """
        hidden = self.graph_scene.undrawable_count()
        if not hidden:
            self._status.setText("Ready.")
            return
        self._status.setText(
            "⚠ %d connection%s cannot be shown (missing target, or an input "
            "name the entity type does not declare). They are preserved on "
            "Apply — click for details."
            % (hidden, "" if hidden == 1 else "s"))
        self._status.setCursor(Qt.PointingHandCursor)
        self._status.mousePressEvent = lambda ev: self._show_undrawable()

    def _show_undrawable(self):
        lines = self.graph_scene.undrawable_report()
        if not lines:
            return
        QMessageBox.information(
            self, "Connections not shown",
            "These connections are in the map but cannot be drawn.\n"
            "They are left untouched when you press Apply.\n\n"
            + "\n".join(lines[:40])
            + ("\n… and %d more" % (len(lines) - 40) if len(lines) > 40 else ""))

    def _apply_style(self):
        self.setStyleSheet("""
            QWidget {
                background: #1c1c20;
                color: #ddd;
                font-family: 'Segoe UI', Arial;
            }
            QPushButton {
                background: #3a3a44;
                border: 1px solid #555;
                border-radius: 4px;
                padding: 4px 10px;
                color: #ddd;
            }
            QPushButton:hover   { background: #4a4a55; }
            QPushButton:pressed { background: #28282e; }
            #applyBtn {
                background: #235523;
                border: 1px solid #3a8a3a;
                color: #aeffae;
            }
            #applyBtn:hover { background: #2c6e2c; }
            #graphToolbar {
                background: #25252a;
                border-bottom: 1px solid #3a3a44;
            }
            #statusBar {
                background: #1a1a1e;
                border-top: 1px solid #2a2a34;
                color: #888;
            }
        """)

    def _on_changed(self):
        self._dirty = True
        self._status.setText("Unsaved changes — press ✔ Apply to save.")
        self._btn_apply.setStyleSheet(
            "background:#3a3312; border:1px solid #a08020; color:#ffe080;")

    def showEvent(self, event):
        """Auto-fit nodes into view on first show so the user doesn't
        have to click Reload to see them."""
        super().showEvent(event)
        if self._first_show:
            self._first_show = False
            # Defer fit_all so the view geometry is fully resolved first
            from PyQt5.QtCore import QTimer
            QTimer.singleShot(0, self.graph_view.fit_all)

    def _reload(self):
        if self._dirty:
            res = QMessageBox.warning(
                self, "Reload Graph?",
                "You have unsaved changes. Reloading will discard them. Continue?",
                QMessageBox.Yes | QMessageBox.No
            )
            if res == QMessageBox.No:
                return

        self.graph_scene = LogicGraphScene(self.editor_state)
        self.graph_scene.connections_changed.connect(self._on_changed)
        self.graph_scene.select_entity.connect(self.select_entity)
        self.graph_view.setScene(self.graph_scene)
        self.graph_scene.set_wire_labels(self._chk_labels.isChecked())
        self.graph_scene.set_all_pins_expanded(self._chk_pins.isChecked())
        self._dirty = False
        self._btn_apply.setStyleSheet("")
        self._refresh_undrawable_notice()

    def _apply(self):
        self.about_to_apply.emit()
        count = self.graph_scene.apply_to_entities()
        self._status.setText(
            f"Applied — {count} connection(s) written to entities.")
        self._btn_apply.setStyleSheet("")
        self._dirty = False
        self.applied.emit()

    def _open_wizard(self):
        try:
            from .logic_wizard import LogicWizard
        except ImportError:
            from editor.logic_wizard import LogicWizard
        wiz = LogicWizard(self.editor_state, self.graph_scene, parent=self)
        if wiz.exec_() == QDialog.Accepted:
            self._status.setText(
                "Wizard applied connections. Press ✔ Apply to save.")
            self._on_changed()

    def closeEvent(self, event):
        """Prompt user if closing with unsaved changes.

        Node positions are published either way: where the graph is laid out is
        not an edit to the map's logic, and losing it because the connections
        were left alone would be its own small betrayal.
        """
        try:
            self.graph_scene.store_positions()
        except Exception:
            pass
        if self._dirty:
            res = QMessageBox.question(
                self, "Unsaved Changes",
                "You have unsaved changes in the logic graph. Apply them before closing?",
                QMessageBox.Apply | QMessageBox.Discard | QMessageBox.Cancel
            )

            if res == QMessageBox.Apply:
                self._apply()
                event.accept()
            elif res == QMessageBox.Discard:
                event.accept()
            else:
                event.ignore()
        else:
            event.accept()

    def get_scene(self) -> LogicGraphScene:
        return self.graph_scene





