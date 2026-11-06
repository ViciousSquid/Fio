"""The scene hierarchy can fold into an always-reachable left edge strip."""

from PyQt5.QtCore import Qt, QEvent, QSize, QTimer
from PyQt5.QtWidgets import QApplication, QDockWidget, QWidget, QHBoxLayout, QLabel, QToolButton


class SceneHierarchyDock(QDockWidget):
    DEFAULT_WIDTH = 200
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
        self._applying = False
        self._owner_size = None
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
        # Window resizes must not change the hierarchy's width (see eventFilter).
        parent.installEventFilter(self)

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
            QTimer.singleShot(0, self._keep_width)

    def showEvent(self, event):
        super().showEvent(event)
        if not self._shown:
            self._shown = True
            QTimer.singleShot(0, lambda: self._resize_to(
                self.STRIP_WIDTH if self.collapsed else self._expanded_width))

    def eventFilter(self, watched, event):
        # The editor has no central widget, so Qt hands any change in window
        # width to the docks, the hierarchy first, up to its maximum. Where a
        # maximised window reaches its final size only after it is shown
        # (Windows, X11 and Wayland, unlike macOS) that left the hierarchy at
        # 400px. Keep its width; the 3D and 2D views take the difference.
        if (watched is self.parentWidget() and event.type() == QEvent.Resize
                and self._shown):
            QTimer.singleShot(0, self._keep_width)
        return False

    def _keep_width(self):
        try:
            owner = self.parentWidget()
            if owner is None or self.isFloating():
                return
        except RuntimeError:
            return  # the window closed before this deferred call ran
        target = self.STRIP_WIDTH if self.collapsed else self._expanded_width
        if self.width() != target:
            self._resize_to(target)
        self._owner_size = owner.size()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        owner = self.parentWidget()
        if owner is None:
            return
        # A new width while the mouse is held and the window keeps its size is
        # the user dragging the separator: that becomes the width to keep.
        # Anything else (Qt settling the layout) must not overwrite it.
        if (self._shown and not self._applying and not self.collapsed
                and not self.isFloating() and owner.size() == self._owner_size
                and QApplication.mouseButtons() & Qt.LeftButton):
            self._expanded_width = min(self.MAX_WIDTH, max(self.MIN_WIDTH, self.width()))
        self._owner_size = owner.size()

    def _resize_to(self, width):
        self._applying = True
        try:
            self._apply_width(width)
        finally:
            self._applying = False

    def _apply_width(self, width):
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
            owner.layout().activate()
            if self.width() != width:
                # On Windows and Linux Qt can ignore the three-column resize;
                # sizing the hierarchy alone then gives the views the rest.
                owner.resizeDocks([self], [width], Qt.Horizontal)
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
        # Showing or hiding the tree relayouts the window afterwards; apply
        # the width again once that has happened.
        QTimer.singleShot(0, self._keep_width)
        self.toggle_button.setFocus(Qt.OtherFocusReason)

    def _update_button(self):
        self.toggle_button.setArrowType(Qt.RightArrow if self.collapsed else Qt.LeftArrow)
        label = "Restore Scene Hierarchy" if self.collapsed else "Collapse Scene Hierarchy"
        self.toggle_button.setToolTip(label)
        self.toggle_button.setAccessibleName(label)
