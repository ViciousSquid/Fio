"""The scene hierarchy can fold into an always-reachable left edge strip."""

from PyQt5.QtCore import Qt, QSize, QTimer
from PyQt5.QtWidgets import QDockWidget, QWidget, QHBoxLayout, QLabel, QToolButton


class SceneHierarchyDock(QDockWidget):
    DEFAULT_WIDTH = 160
    MIN_WIDTH = 140
    MAX_WIDTH = 400
    STRIP_WIDTH = 28

    def __init__(self, hierarchy, parent):
        super().__init__("Scene Hierarchy", parent)
        self.setObjectName("SceneDock")
        self.setWidget(hierarchy)
        self.collapsed = False
        self._expanded_width = self.DEFAULT_WIDTH
        self._shown = False
        self.setMinimumWidth(self.MIN_WIDTH)
        self.setMaximumWidth(self.MAX_WIDTH)

        header = QWidget(self)
        header.setFixedHeight(self.STRIP_WIDTH)
        header.setStyleSheet("background: #1b1b1f;")
        row = QHBoxLayout(header)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(0)
        self.title_label = QLabel("Scene", header)
        self.title_label.setContentsMargins(8, 0, 0, 0)
        row.addWidget(self.title_label, 1)
        self.toggle_button = QToolButton(header)
        self.toggle_button.setObjectName("SceneHierarchyToggle")
        self.toggle_button.setFixedSize(self.STRIP_WIDTH, self.STRIP_WIDTH)
        self.toggle_button.setAutoRaise(True)
        self.toggle_button.clicked.connect(lambda: self.set_collapsed(not self.collapsed))
        row.addWidget(self.toggle_button)
        self.setTitleBarWidget(header)
        self._update_button()

    @property
    def expanded_width(self):
        return self._expanded_width if self.collapsed else max(self.MIN_WIDTH, self.width())

    def sizeHint(self):
        hint = super().sizeHint()
        return QSize(self.STRIP_WIDTH if self.collapsed else self._expanded_width, hint.height())

    def restore_width(self, width):
        self._expanded_width = min(self.MAX_WIDTH, max(self.MIN_WIDTH, int(width)))
        if not self.collapsed:
            self._resize_to(self._expanded_width)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown:
            self._shown = True
            QTimer.singleShot(0, lambda: self._resize_to(
                self.STRIP_WIDTH if self.collapsed else self._expanded_width))

    def _resize_to(self, width):
        owner = self.parentWidget()
        owner.layout().activate()
        views = [getattr(owner, name, None) for name in ('view_3d_dock', 'right_dock')]
        if all(view is not None and owner.dockWidgetArea(view) == Qt.RightDockWidgetArea
               for view in views):
            # With no central widget, resizing only the hierarchy gives it
            # surplus space. Size all three columns while keeping their ratio.
            shared = max(1, sum(view.width() for view in views))
            remaining = max(1, shared + self.width() - width)
            sizes = [width, round(remaining * views[0].width() / shared),
                     round(remaining * views[1].width() / shared)]
            owner.resizeDocks([self, *views], sizes, Qt.Horizontal)
        else:
            owner.resizeDocks([self], [width], Qt.Horizontal)

    def dock_left(self):
        owner = self.parentWidget()
        if self.isFloating() or owner.dockWidgetArea(self) != Qt.LeftDockWidgetArea:
            self.setFloating(False)
            owner.addDockWidget(Qt.LeftDockWidgetArea, self)

    def set_collapsed(self, collapsed):
        collapsed = bool(collapsed)
        if collapsed == self.collapsed:
            return
        if collapsed:
            self._expanded_width = min(self.MAX_WIDTH, max(self.MIN_WIDTH, self.width()))
            self.dock_left()
        self.collapsed = collapsed
        self.widget().setVisible(not collapsed)
        self.title_label.setVisible(not collapsed)
        self.setMinimumWidth(self.STRIP_WIDTH if collapsed else self.MIN_WIDTH)
        self.setMaximumWidth(self.STRIP_WIDTH if collapsed else self.MAX_WIDTH)
        self._update_button()
        self._resize_to(self.STRIP_WIDTH if collapsed else self._expanded_width)
        self.toggle_button.setFocus(Qt.OtherFocusReason)

    def _update_button(self):
        self.toggle_button.setArrowType(Qt.RightArrow if self.collapsed else Qt.LeftArrow)
        label = "Restore Scene Hierarchy" if self.collapsed else "Collapse Scene Hierarchy"
        self.toggle_button.setToolTip(label)
        self.toggle_button.setAccessibleName(label)
