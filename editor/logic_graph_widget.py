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
    QPainter, QPen, QBrush, QColor, QFont, QPainterPath,
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
    'logic_keyvalue': QColor(170, 140,  50),
}
C_HDR_DEFAULT = QColor(85, 85, 95)


# ── Pin ───────────────────────────────────────────────────────────────────────

class PinItem(QGraphicsEllipseItem):
    def __init__(self, node: EntityNodeItem, name: str,
                 is_output: bool, row: int):
        pr = node._pin_r
        super().__init__(-pr, -pr, pr * 2, pr * 2)
        self.node      = node
        self.pin_name  = name
        self.is_output = is_output
        self.setParentItem(node)

        y = node._nh_hdr + row * node._nh_pin + node._nh_pin // 2
        x = node._nw if is_output else 0
        self.setPos(x, y)

        col = C_OUT if is_output else C_IN
        self.setBrush(QBrush(col))
        self.setPen(QPen(Qt.NoPen))
        self.setZValue(4)
        self.setCursor(Qt.CrossCursor)
        self.setAcceptHoverEvents(True)

    def scene_center(self) -> QPointF:
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
        self._update_pen(False)
        self.setZValue(1)
        self.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.setAcceptHoverEvents(True)
        self.refresh()

    def _update_pen(self, selected: bool):
        col = C_CONN_SEL if selected else C_CONN
        self.setPen(QPen(col, 2.2, Qt.SolidLine, Qt.RoundCap))

    def refresh(self):
        if self.src_pin and self.dst_pin:
            self._bezier(self.src_pin.scene_center(),
                         self.dst_pin.scene_center())

    def _bezier(self, p1: QPointF, p2: QPointF):
        path = QPainterPath(p1)
        cx = max(80, abs(p2.x() - p1.x()) * 0.55)
        path.cubicTo(p1 + QPointF(cx, 0),
                     p2 - QPointF(cx, 0), p2)
        self.setPath(path)

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

        outputs = get_output_names(entity_type) if IO_AVAILABLE else []
        inputs  = get_input_names(entity_type)  if IO_AVAILABLE else []

        rows     = max(len(outputs), len(inputs), 1)
        self._h  = self._nh_hdr + rows * self._nh_pin + self._nh_pad

        self.out_pins: Dict[str, PinItem] = {}
        self.in_pins:  Dict[str, PinItem] = {}

        for i, name in enumerate(outputs):
            self.out_pins[name] = PinItem(self, name, True,  i)
        for i, name in enumerate(inputs):
            self.in_pins[name]  = PinItem(self, name, False, i)

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

        p.setPen(QPen(Qt.white))
        p.setFont(_ui_font(bold=True))
        p.drawText(QRectF(10, 0, nw - 14, hh),
                   Qt.AlignVCenter | Qt.AlignLeft, self.entity_name)

        p.setPen(QPen(QColor(255, 255, 255, 130)))
        p.setFont(_ui_font(delta=-2))
        p.drawText(QRectF(0, 0, nw - 8, hh),
                   Qt.AlignVCenter | Qt.AlignRight, self.entity_type)

        p.setPen(QPen(QColor(255, 255, 255, 25), 1))
        p.drawLine(QPointF(0, hh), QPointF(nw, hh))

        p.setFont(_ui_font(delta=-1))
        for i, name in enumerate(self.out_pins):
            y = hh + i * row
            p.setPen(QPen(C_OUT.lighter(140)))
            p.drawText(QRectF(0, y, nw - pr * 2 - 6, row),
                       Qt.AlignVCenter | Qt.AlignRight, name)

        for i, name in enumerate(self.in_pins):
            y = hh + i * row
            p.setPen(QPen(C_IN.lighter(140)))
            p.drawText(QRectF(pr * 2 + 6, y, nw - pr * 2 - 12, row),
                       Qt.AlignVCenter | Qt.AlignLeft, name)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionHasChanged:
            scene = self.scene()
            if scene:
                scene.refresh_node_connections(self)
        return super().itemChange(change, value)

    def contextMenuEvent(self, ev):
        menu = QMenu()
        lbl = menu.addAction(f"{self.entity_name}  [{self.entity_type}]")
        lbl.setEnabled(False)
        menu.addSeparator()
        menu.addAction(
            "Clear all connections from this node",
            lambda: self.scene().clear_node_connections(self) if self.scene() else None
        )
        menu.exec_(ev.screenPos())


# ── Scene ─────────────────────────────────────────────────────────────────────

class LogicGraphScene(QGraphicsScene):
    connections_changed = pyqtSignal()

    def __init__(self, editor_state, parent=None):
        super().__init__(parent)
        self.editor_state = editor_state
        self.setBackgroundBrush(QBrush(QColor(28, 28, 32)))

        self._nodes:       Dict[str, EntityNodeItem] = {}
        self._connections: List[ConnectionItem]      = []
        self._drag_line:   Optional[DragLine]        = None
        self._drag_src:    Optional[PinItem]         = None

        self._build_nodes()
        self._build_connections()
        self._fix_source_target_order()

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
        name_to_node: Dict[str, EntityNodeItem] = {
            n.entity_name: n for n in self._nodes.values()
        }
        # Also build an ID-based lookup for connections that carry target_id
        id_to_node: Dict[str, EntityNodeItem] = {}
        for eid, node in self._nodes.items():
            if eid:
                id_to_node[eid] = node

        all_entities = list(self.editor_state.things) + list(self.editor_state.brushes)
        for entity in all_entities:
            src_name = self._entity_name(entity)
            src_node = name_to_node.get(src_name)
            if not src_node:
                continue
            for conn in self._entity_connections(entity):
                if not hasattr(conn, 'output_name'):
                    continue
                # Prefer ID lookup, fall back to name
                dst_node = None
                target_id = getattr(conn, 'target_id', '')
                if target_id:
                    dst_node = id_to_node.get(target_id)
                if dst_node is None:
                    dst_node = name_to_node.get(conn.target_name)
                if not dst_node:
                    continue
                src_pin = src_node.out_pins.get(conn.output_name)
                dst_pin = dst_node.in_pins.get(conn.input_name)
                if src_pin and dst_pin:
                    ci = ConnectionItem(src_pin, dst_pin, conn)
                    self.addItem(ci)
                    self._connections.append(ci)

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
                ev.accept()
                return
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev):
        if self._drag_line and self._drag_src:
            self._drag_line.move_to(ev.scenePos())
            ev.accept()
            return
        super().mouseMoveEvent(ev)

    def mouseReleaseEvent(self, ev):
        if self._drag_line and self._drag_src:
            dst = self._pin_at(ev.scenePos())
            if dst and not dst.is_output and dst.node is not self._drag_src.node:
                self._create_connection(self._drag_src, dst)
            self.removeItem(self._drag_line)
            self._drag_line = None
            self._drag_src  = None
            ev.accept()
            return
        super().mouseReleaseEvent(ev)

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
        self.connections_changed.emit()

    def remove_connection(self, ci: ConnectionItem):
        if ci in self._connections:
            self._connections.remove(ci)
        self.removeItem(ci)
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
        if not IO_AVAILABLE:
            return 0
        all_entities = list(self.editor_state.things) + list(self.editor_state.brushes)
        for entity in all_entities:
            if hasattr(entity, 'properties'):
                entity.properties['_io_connections'] = []
            else:
                entity['_io_connections'] = []
        count = 0
        for ci in self._connections:
            if ci.conn_obj is None:
                continue
            src_entity = ci.src_pin.node.entity
            if hasattr(src_entity, 'properties'):
                src_entity.properties['_io_connections'].append(ci.conn_obj)
            else:
                src_entity.setdefault('_io_connections', []).append(ci.conn_obj)
            count += 1
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
        """Scale node geometry by shifting the global application font."""
        f = QApplication.font()
        new_size = max(6, f.pointSize() + delta)
        f.setPointSize(new_size)
        QApplication.setFont(f)
        
        window = self.window()
        if hasattr(window, '_reload'):
            window._reload()

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


# ── Window ────────────────────────────────────────────────────────────────────

class LogicGraphWindow(QWidget):
    applied = pyqtSignal()

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
        tb_lay.addStretch()

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

        for w in (btn_refresh, btn_wizard, self._btn_apply):
            tb_lay.addWidget(w)

        root.addWidget(tb)

        self.graph_scene = LogicGraphScene(self.editor_state)
        self.graph_scene.connections_changed.connect(self._on_changed)
        self.graph_view  = LogicGraphView(self.graph_scene)
        root.addWidget(self.graph_view)

        self._status = QLabel("Ready.")
        self._status.setObjectName("statusBar")
        self._status.setFixedHeight(fm.height() + 10)
        self._status.setContentsMargins(10, 0, 10, 0)
        root.addWidget(self._status)

        QShortcut(QKeySequence("Ctrl+S"), self).activated.connect(self._apply)

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
        self.graph_view.setScene(self.graph_scene)
        self._status.setText("Graph reloaded from scene.")
        self._dirty = False
        self._btn_apply.setStyleSheet("")

    def _apply(self):
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
        """Prompt user if closing with unsaved changes."""
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





