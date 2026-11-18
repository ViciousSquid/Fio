"""Consistent property forms across Qt's native platform styles."""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QComboBox, QFormLayout, QSizePolicy


#: A dropdown with this property set keeps its own width -- as wide as its
#: options, e.g. a single letter -- instead of growing to fill the row.
SNUG = 'fio_snug'


def configure_property_forms(widget):
    """Fill the panel from the left, wrapping long rows on narrow docks.

    macOS defaults to centred forms with fields kept at their size hints.
    Apply this after building a page, including lazily built plugin tabs.
    """
    for form in widget.findChildren(QFormLayout):
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setLabelAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
    for combo in widget.findChildren(QComboBox):
        if combo.property(SNUG):
            continue                      # sized to its options (see SNUG)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(12)
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
