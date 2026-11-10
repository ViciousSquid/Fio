"""A list that says when its rows change (M4).

The render and entity tables hold the world's row set -- the brush and
entity lists -- and used to recognise an unchanged row set by comparing a
tuple of every row with the one they last reconciled: an identity test per
row, every frame, on the logic thread, growing with the world. A
:class:`RowList` instead takes a new :attr:`~RowList.row_version` whenever a
row is added, removed, replaced or reordered, and a table that holds the
version it reconciled recognises the same rows with one comparison.

The version is drawn from one process-wide counter, so every change gets a
value never seen before: two threads changing the list at once cannot leave
it at a version a reader already holds. It is taken after the change, so a
reader that sees the old version sees the old rows or is one frame late,
never wrong for good.

Only the row set is versioned. A change inside a row (a brush dict edited)
reaches the tables through the editor epoch and the change journal, as
before.
"""

import itertools

_versions = itertools.count(1)


class RowList(list):
    """A ``list`` whose :attr:`row_version` moves with every change to its
    rows. Reading and iterating are the plain list's."""

    __slots__ = ('row_version',)

    def __init__(self, *args):
        super().__init__(*args)
        self.row_version = next(_versions)

    def _changed(self):
        self.row_version = next(_versions)

    def append(self, item):
        super().append(item)
        self.row_version = next(_versions)

    def extend(self, items):
        super().extend(items)
        self.row_version = next(_versions)

    def insert(self, index, item):
        super().insert(index, item)
        self.row_version = next(_versions)

    def remove(self, item):
        super().remove(item)
        self.row_version = next(_versions)

    def pop(self, *args):
        item = super().pop(*args)
        self.row_version = next(_versions)
        return item

    def clear(self):
        super().clear()
        self.row_version = next(_versions)

    def sort(self, *args, **kwargs):
        super().sort(*args, **kwargs)
        self.row_version = next(_versions)

    def reverse(self):
        super().reverse()
        self.row_version = next(_versions)

    def __setitem__(self, index, value):
        super().__setitem__(index, value)
        self.row_version = next(_versions)

    def __delitem__(self, index):
        super().__delitem__(index)
        self.row_version = next(_versions)

    def __iadd__(self, items):
        super().__iadd__(items)
        self.row_version = next(_versions)
        return self

    def __imul__(self, factor):
        super().__imul__(factor)
        self.row_version = next(_versions)
        return self


def row_list(rows):
    """*rows* as a :class:`RowList` when it is a plain ``list`` (copied);
    anything else -- a RowList already, or another list type a caller has
    its own reasons for -- as it is (a table then compares it row by row)."""
    return RowList(rows) if type(rows) is list else rows
