"""The brush group dialog: Scene Hierarchy > Add to Group... / Edit Group...

One dialog for both. It names the group the chosen brushes go in -- a new
name makes a group, an existing name joins it -- and lists that group's
members, each of which can be unticked to take it out. When the chosen
brushes are already grouped, typing a new name renames their group.

"Brushes" here are any groupable objects: brush dicts and entities
(``Thing``) alike -- see :mod:`editor.brush_groups`.
"""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem,
    QPushButton, QVBoxLayout,
)

from editor import brush_groups

_STYLE = """
    QDialog, QLabel { background-color: #2b2b2b; color: #ddd; }
    QComboBox, QListWidget { background-color: #333; color: #ddd;
                             border: 1px solid #555; border-radius: 4px; padding: 3px; }
    QPushButton { background-color: #333; color: #ddd; border: 1px solid #555;
                  border-radius: 4px; padding: 5px 12px; }
    QPushButton:hover { background-color: #444; }
"""


class BrushGroupDialog(QDialog):
    def __init__(self, parent, all_brushes, chosen, display_name):
        """*chosen*: the brushes the menu was opened on. *display_name*:
        ``brush -> str`` for the member list."""
        super().__init__(parent)
        self.setWindowTitle("Group")
        self.setStyleSheet(_STYLE)
        self.resize(340, 380)
        self._brushes = list(all_brushes)
        self._chosen = list(chosen)
        self._display_name = display_name
        self.ungroup = False
        self._listed = []

        current = [brush_groups.group_of(b) for b in self._chosen]
        #: The group being edited: the one the chosen brushes are already in.
        self.editing = next((name for name in current if name), '')

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Group name:"))
        self.name_box = QComboBox()
        self.name_box.setEditable(True)
        self.name_box.addItems(list(brush_groups.groups(self._brushes)))
        self.name_box.setCurrentText(
            self.editing or brush_groups.unique_name(self._brushes))
        self.name_box.setToolTip("Type a new name, or pick a group to join")
        self.name_box.currentTextChanged.connect(self._refresh)
        layout.addWidget(self.name_box)

        self.hint = QLabel()
        self.hint.setWordWrap(True)
        self.hint.setStyleSheet("color: #aaa;")
        layout.addWidget(self.hint)

        layout.addWidget(QLabel("Members of the group (untick to take one out):"))
        self.member_list = QListWidget()
        layout.addWidget(self.member_list)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.ungroup_btn = QPushButton("Ungroup")
        self.ungroup_btn.setToolTip("Dissolve this group: its members stay, detached")
        self.ungroup_btn.clicked.connect(self._ungroup)
        buttons.addButton(self.ungroup_btn, QDialogButtonBox.DestructiveRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    # -- what the dialog says ---------------------------------------------------

    def name(self):
        return self.name_box.currentText().strip()

    def renaming(self):
        """The chosen brushes' group is being given a name no group has."""
        name = self.name()
        return bool(self.editing and name and name != self.editing
                    and name not in brush_groups.groups(self._brushes))

    def _candidates(self):
        """The brushes the list offers: the named group's members (the
        edited group's, when it is being renamed) and the chosen brushes."""
        source = self.editing if self.renaming() else self.name()
        listed = brush_groups.members(self._brushes, source)
        seen = {id(b) for b in listed}
        return listed + [b for b in self._chosen if id(b) not in seen]

    def _refresh(self, *_args):
        name = self.name()
        existing = brush_groups.groups(self._brushes)
        self.hint.setStyleSheet("color: #aaa;")
        if not name:
            self.hint.setText("Give the group a name.")
        elif self.renaming():
            self.hint.setText("Renames group '%s' to '%s'." % (self.editing, name))
        elif name in existing and name != self.editing:
            self.hint.setText("Adds the selected objects to group '%s'." % name)
        elif name == self.editing:
            self.hint.setText("Grouped objects always move together in the "
                              "editor; grouped brushes in Play too.")
        else:
            self.hint.setText("Makes a new group '%s'." % name)
        self.member_list.clear()
        # Rows hold an index: Qt would copy a dict stored as item data.
        self._listed = self._candidates()
        for row, brush in enumerate(self._listed):
            item = QListWidgetItem(self._display_name(brush))
            item.setData(Qt.UserRole, row)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            self.member_list.addItem(item)
        self.ungroup_btn.setEnabled(bool(self.editing))

    def kept(self):
        """The ticked brushes: the group's members once OK is pressed."""
        items = (self.member_list.item(i) for i in range(self.member_list.count()))
        return [self._listed[item.data(Qt.UserRole)] for item in items
                if item.checkState() == Qt.Checked]

    def dropped(self):
        items = (self.member_list.item(i) for i in range(self.member_list.count()))
        return [self._listed[item.data(Qt.UserRole)] for item in items
                if item.checkState() != Qt.Checked]

    def allowed(self):
        """A group holds at most one mover."""
        return self.ungroup or not brush_groups.too_many_movers(self.kept())

    def accept(self):
        if not self.allowed():
            self.hint.setStyleSheet("color: #ff6b6b;")
            self.hint.setText(brush_groups.MAX_MOVERS_MESSAGE
                              + ": untick one of the movers.")
            return
        super().accept()

    def _ungroup(self):
        self.ungroup = True
        self.accept()

    def apply(self):
        """Write the dialog's answer into the brushes; ``True`` if anything
        was (or may have been) changed."""
        if self.ungroup:
            brush_groups.set_group(
                brush_groups.members(self._brushes, self.editing), '')
            return True
        name = self.name()
        if not name or not self.allowed():
            return False
        brush_groups.set_group(self.dropped(), '')
        brush_groups.set_group(self.kept(), name)
        return True
