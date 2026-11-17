"""The terrain as something the editor can have selected.

The selection is a list of brushes (dicts) and entities, and a great deal of
the editor walks it expecting exactly those. The terrain is neither, so it is
not put in that list: the editor has it selected *instead of* objects
(``MainWindow.terrain_selected``), and this one stand-in is what the property
editor is handed to show its panel -- the lock and the Edit button.
"""


class TerrainHandle:
    """What the property editor shows when the terrain is selected."""

    __slots__ = ()

    def __repr__(self):
        return 'TERRAIN'


#: The one stand-in: there is one terrain to a map.
TERRAIN = TerrainHandle()
