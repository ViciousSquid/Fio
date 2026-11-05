"""Ctrl-K: drag a selected entity to a destination using native link semantics."""
import math
import re
import uuid

from PyQt5.QtCore import QEvent, QObject, QPointF, Qt
from PyQt5.QtGui import QColor, QCursor, QPainter, QPen, QPolygonF
from PyQt5.QtWidgets import QAction, QApplication, QWidget

from editor.io_system import (OutputConnection, add_connection, bump_io_revision,
                              get_connections, get_entity_type_for_io,
                              get_inputs, get_outputs)
from engine.change_journal import touch


def properties(entity):
    return entity if isinstance(entity, dict) else entity.properties


def connect_entities(owner, source, target):
    """Validate first, then create exactly one undoable native connection."""
    if owner.view_3d.play_mode:
        raise ValueError('Entity links can only be edited outside Play mode.')
    if source is target:
        raise ValueError('Choose another entity as the destination.')
    scene = list(owner.state.brushes) + list(owner.state.things)
    if not all(any(item is entity for item in scene) for entity in (source, target)):
        raise ValueError('Both entities must still be in the level.')
    src, dst = properties(source), properties(target)
    if src.get('lock') or dst.get('lock'):
        raise ValueError('Unlock both entities before linking them.')
    kind, target_kind = get_entity_type_for_io(source), get_entity_type_for_io(target)
    names = {properties(item).get('name') for item in scene}
    name = str(dst.get('name') or '').strip()
    if not name:
        base = re.sub(r'[^A-Za-z0-9_]', '_', target_kind) or 'Entity'
        number = 1
        while f'{base}_{number}' in names:
            number += 1
        name = f'{base}_{number}'
    field = None
    if kind == 'path_node' and target_kind == 'path_node':
        field = 'next_node'
    elif kind == 'deathmatchnode' and target_kind == 'deathmatchnode':
        field = 'links'
    elif kind == 'monster' and target_kind == 'path_node':
        field = 'patrol_target'
    elif kind == 'logic_camera' and target_kind == 'path_node':
        field = 'path_target'
    elif kind == 'logic_spawner' and target_kind == 'path_node':
        field = 'target_node'
    elif kind == 'trigger' and src.get('trigger_action') == 'teleport' and target_kind == 'path_node':
        field = 'target_node'
    elif kind == 'portal' and target_kind == 'portal':
        field = 'portal_target'
    connection = None
    if field is not None:
        if any(item is not target and properties(item).get('name') == name for item in scene):
            raise ValueError('Give the destination a unique name before creating a name-based link.')
        linked = re.split(r'[,\s]+', str(src.get('links', '')).strip()) if field == 'links' else []
        already_linked = name in linked if field == 'links' else src.get(field) == name
        if field == 'links' and src.get('name'):
            already_linked |= src['name'] in re.split(r'[,\s]+', str(dst.get('links', '')).strip())
        if already_linked and (field != 'patrol_target' or src.get('patrol')):
            return f'Already linked to {name}.'
        description = f'{field} → {name}'
    else:
        outputs = get_outputs(kind)
        inputs = [entry for entry in get_inputs(target_kind) if not entry.param_type]
        if not outputs or not inputs:
            raise ValueError('These entity types have no compatible navigation link or parameter-free I/O action.')
        preferred_outputs = ('OnTrigger', 'OnTimer', 'OnPressed', 'OnUse', 'OnDamaged', 'OnDeath')
        preferred_inputs = {'door': 'Open', 'mover': 'Open', 'monster': 'Wake',
                            'light': 'TurnOn', 'logic_relay': 'Trigger', 'logic_timer': 'Enable',
                            'audio': 'PlaySound'}
        output = min(outputs, key=lambda d: preferred_outputs.index(d.name) if d.name in preferred_outputs else len(preferred_outputs)).name
        choices = (preferred_inputs.get(target_kind), 'Trigger', 'Toggle', 'Enable', 'TurnOn', 'Open', 'Wake', 'Start', 'Show')
        input_name = min(inputs, key=lambda d: choices.index(d.name) if d.name in choices else len(choices)).name
        target_id = dst.get('id', '')
        for previous in get_connections(source):
            same_target = (previous.target_id == target_id if previous.target_id and target_id
                           else previous.target_name == name)
            if same_target and previous.output_name == output and previous.input_name == input_name:
                return f'Already linked: {output} → {name}.{input_name}.'
        connection = OutputConnection(output, name, input_name, target_id=target_id)
        description = f'{output} → {name}.{input_name}'
    owner.save_state()
    dst['name'] = name
    if not dst.get('id'):
        dst['id'] = str(uuid.uuid4())
    if connection is not None:
        connection.target_id = dst['id']
        add_connection(source, connection)
    elif field == 'links':
        linked = [part for part in linked if part]
        src['links'] = ','.join(linked + [name])
        bump_io_revision()
    else:
        src[field] = name
        if field == 'patrol_target':
            src['patrol'] = True
        if field == 'portal_target':
            from editor.portal_links import sync_group_to
            sync_group_to(source, owner.state.things)
        bump_io_revision()
    touch(source)
    touch(target)
    owner.update_all_ui()
    return f'Linked {description}'


class LinkOverlay(QWidget):
    def __init__(self, view, tool):
        super().__init__(view)
        self.tool = tool
        self.view = view
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.NoFocus)

    def paintEvent(self, event):
        tool = self.tool
        if tool.source is None:
            return
        start = tool.screen_position(self.view, tool.source)
        if start is None:
            return
        end = (tool.screen_position(self.view, tool.target) if tool.target is not None
               else QPointF(tool.pointer))
        if end is None:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor('#64dc78' if tool.target is not None else '#f08000')
        painter.setPen(QPen(color, 2, Qt.DashLine))
        painter.drawLine(start, end)
        angle = math.atan2(end.y() - start.y(), end.x() - start.x())
        wings = [QPointF(end.x() - 14 * math.cos(angle + offset),
                         end.y() - 14 * math.sin(angle + offset)) for offset in (-0.45, 0.45)]
        painter.setBrush(color)
        painter.drawPolygon(QPolygonF([end] + wings))
        painter.drawText(12, 24, 'Link entities: drag to a destination · release to connect · Esc / right-click to cancel')


class EntityLinkTool(QObject):
    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.source = self.target = self.view = None
        self.pointer = QPointF()
        self.dragging = False
        self._cursor = None
        self.views = tuple(getattr(owner, name) for name in ('view_top', 'view_side', 'view_front', 'view_3d'))
        self.overlays = {view: LinkOverlay(view, self) for view in self.views}
        for overlay in self.overlays.values():
            overlay.hide()
        for view in self.views:
            view.installEventFilter(self)
        owner.installEventFilter(self)
        action = owner.link_entity_action = QAction('Link selected entity…', owner)
        action.setShortcut('Ctrl+K')
        action.setToolTip('Select an entity, press Ctrl+K, then drag to another entity and release.')
        action.triggered.connect(self.begin)
        owner.addAction(action)

    @staticmethod
    def screen_position(view, entity):
        pos = entity.get('pos', (0, 0, 0)) if isinstance(entity, dict) else entity.pos
        if hasattr(view, '_world_to_screen'):
            point = view._world_to_screen(pos)
            return QPointF(point) if point is not None else None
        axes = {'x': 0, 'y': 1, 'z': 2}
        first, second = view.get_axes()
        return QPointF(view.world_to_screen(QPointF(pos[axes[first]], pos[axes[second]])))

    def begin(self):
        self.cancel()
        if self.owner.view_3d.play_mode:
            self.owner.show_toast('Leave Play mode to link entities.')
            return
        selected = self.owner.state.selected_objects
        if len(selected) != 1:
            self.owner.show_toast('Select one source entity, then press Ctrl+K.')
            return
        self.source = selected[0]
        self.owner.view_3d.set_editor_freelook(False)
        focus = QApplication.focusWidget()
        view = next((v for v in self.views if v is focus or (focus is not None and v.isAncestorOf(focus))), None)
        if view is not None:
            self.activate_view(view, view.mapFromGlobal(QCursor.pos()))
        self.owner.show_toast('Drag to the destination and release. Esc cancels.')

    def activate_view(self, view, pos):
        if self.view is not view:
            if self.view is not None:
                self.overlays[self.view].hide()
                self.view.setCursor(self._cursor)
            self.view = view
            self._cursor = view.cursor()
            view.setCursor(Qt.CrossCursor)
        self.pointer = QPointF(pos)
        overlay = self.overlays[view]
        overlay.setGeometry(view.rect())
        overlay.show()
        overlay.raise_()
        overlay.update()

    def cancel(self):
        if self.view is not None:
            self.overlays[self.view].hide()
            self.view.setCursor(self._cursor)
        self.source = self.target = self.view = None
        self.dragging = False

    def _target_at(self, view, pos):
        if hasattr(view, 'get_object_at_3d'):
            target = view.get_object_at_3d(pos.x(), pos.y())
        else:
            target = view.get_object_at(pos)
        return target if target is not self.source else None

    def eventFilter(self, watched, event):
        if self.source is None:
            return False
        if self.owner.view_3d.play_mode or not any(self.source is item for item in list(self.owner.state.things) + list(self.owner.state.brushes)):
            self.cancel()
            return False
        kind = event.type()
        if kind == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.cancel()
            return True
        if watched not in self.views:
            return False
        if kind == QEvent.Resize and watched is self.view:
            self.overlays[watched].setGeometry(watched.rect())
        elif kind == QEvent.MouseButtonPress:
            if event.button() == Qt.RightButton:
                self.cancel()
                return True
            if event.button() == Qt.LeftButton:
                self.dragging = True
                self.activate_view(watched, event.pos())
                return True
        elif kind == QEvent.MouseMove:
            self.target = self._target_at(watched, event.pos())
            self.activate_view(watched, event.pos())
            return True
        elif kind == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
            if self.dragging:
                source, target = self.source, self._target_at(watched, event.pos())
                self.cancel()
                if target is not None:
                    try:
                        message = connect_entities(self.owner, source, target)
                    except ValueError as exc:
                        message = str(exc)
                    self.owner.show_toast(message)
            return True
        return False
