"""macOS: forms and tab bars are laid out as on Windows (macos_app)."""
import pytest

pytestmark = pytest.mark.qt

from PyQt5.QtWidgets import QApplication, QStyle, QStyleFactory, QFormLayout
from PyQt5.QtCore import Qt

import macos_app


@pytest.fixture(scope='module')
def style():
    app = QApplication.instance() or QApplication([])
    style = macos_app.windows_layout_style()
    assert style is not None
    yield style


def test_layout_hints_are_the_windows_styles(style):
    windows = QStyleFactory.create('Windows')
    for name in macos_app._LAYOUT_HINTS:
        hint = getattr(QStyle, name)
        assert style.styleHint(hint) == windows.styleHint(hint), name


def test_fields_stretch_and_forms_start_at_the_left(style):
    assert style.styleHint(QStyle.SH_FormLayoutFieldGrowthPolicy) == \
        QFormLayout.AllNonFixedFieldsGrow
    assert style.styleHint(QStyle.SH_FormLayoutFormAlignment) & Qt.AlignLeft
    assert style.styleHint(QStyle.SH_TabBar_Alignment) == Qt.AlignLeft


def test_other_hints_still_come_from_the_native_style(style):
    native = style.baseStyle()
    for hint in (QStyle.SH_ScrollBar_Transient, QStyle.SH_Menu_SpaceActivatesItem,
                 QStyle.SH_ToolTipLabel_Opacity):
        assert style.styleHint(hint) == native.styleHint(hint)
