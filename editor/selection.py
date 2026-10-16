"""Short-lived selection membership for drawing, without a world-sized scan.

Build once per draw, so edits, replacements, and selection mutations never need
cache invalidation. Stable brush ids narrow equality checks; actual selected
objects always match by identity. Equal copied dictionaries still match just
as they did with list membership, including missing or duplicate ids.
"""


class SelectionLookup:
    def __init__(self, objects):
        self.objects = list(objects)
        self.identities = {id(obj) for obj in self.objects}
        self.brushes = {}
        self.other_equality = []
        for obj in self.objects:
            if type(obj) is dict:
                try:
                    self.brushes.setdefault(obj.get('id'), []).append(obj)
                except TypeError:  # malformed/unhashable ids retain list semantics
                    self.other_equality.append(obj)
            elif type(obj).__eq__ is not object.__eq__:
                self.other_equality.append(obj)

    def __contains__(self, obj):
        if id(obj) in self.identities:
            return True
        if type(obj) is dict:
            try:
                candidates = self.brushes.get(obj.get('id'), ())
            except TypeError:
                return obj in self.objects
            return obj in candidates or obj in self.other_equality
        if type(obj).__eq__ is object.__eq__:
            return obj in self.other_equality
        return obj in self.objects
