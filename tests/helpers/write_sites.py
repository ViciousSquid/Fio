"""Where Fio's code writes the brush fields the unified model will change.

Stage 0 of ``docs/brush-model-proposal.md``: the fields that are stored
twice (face material in per-tag dicts *and* on convex planes), mean several
things (``pos``/``size``: box geometry, convex bound, mover transform) or
are play's state kept in the authored dict (``original_pos``, the mover's
spin). Later stages move every write to one owner; until then this is a
ratchet -- a count per module and field that may only go down, so a new
write to a duplicated field is a reviewed decision rather than an accident.

Counted statically, so it is conservative: a write of ``'pos'`` into a
dict that is not a brush (a Thing's properties, a preview record) counts
too. What counts as a write:

* an assignment, augmented assignment or ``del`` whose target subscripts a
  field (``b['pos'] = ..``, ``b['pos'][1] += ..``, ``b['textures'][t] = ..``);
* ``.setdefault(field, ..)`` and ``.pop(field, ..)``;
* ``.update(field=..)`` and ``.update({field: ..})``.

Dict literals that *create* a brush are not writes.
"""

import ast
import glob
import os
from collections import Counter

FIELDS = ('pos', 'size', 'geometry', 'textures', 'uv_scale', 'uv_shift',
          'uv_angle', 'uv_natural', 'texture_tiling', 'original_pos',
          '_rot_angle', 'rotation_yaw')

#: Desktop sources (the Android player is out of scope, as are tests and
#: developer tools).
SOURCE_DIRS = ('engine', 'editor', 'plugins')


def _field(node):
    if isinstance(node, ast.Constant) and node.value in FIELDS:
        return node.value
    return None


def _target_field(target):
    """The field a write target writes, or ``None``."""
    node = target
    while isinstance(node, ast.Subscript):
        field = _field(node.slice)
        if field is not None:
            return field
        node = node.value
    return None


class _Visitor(ast.NodeVisitor):
    def __init__(self):
        self.found = Counter()

    def _targets(self, targets):
        for target in targets:
            if isinstance(target, (ast.Tuple, ast.List)):
                self._targets(target.elts)
                continue
            field = _target_field(target)
            if field is not None:
                self.found[field] += 1

    def visit_Assign(self, node):
        self._targets(node.targets)
        self.generic_visit(node)

    def visit_AugAssign(self, node):
        self._targets([node.target])
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        self._targets([node.target])
        self.generic_visit(node)

    def visit_Delete(self, node):
        self._targets(node.targets)
        self.generic_visit(node)

    def visit_Call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute):
            if func.attr in ('setdefault', 'pop') and node.args:
                field = _field(node.args[0])
                if field is not None:
                    self.found[field] += 1
            elif func.attr == 'update':
                for keyword in node.keywords:
                    if keyword.arg in FIELDS:
                        self.found[keyword.arg] += 1
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        for key in arg.keys:
                            field = _field(key)
                            if field is not None:
                                self.found[field] += 1
        self.generic_visit(node)


def source_files(root):
    files = sorted(glob.glob(os.path.join(root, '*.py')))
    for folder in SOURCE_DIRS:
        for path in sorted(glob.glob(os.path.join(root, folder, '**', '*.py'),
                                     recursive=True)):
            parts = os.path.relpath(path, root).split(os.sep)
            if 'tests' in parts or '__pycache__' in parts:
                continue
            files.append(path)
    return files


def scan(root):
    """``{field: {module path: count}}`` over the desktop sources."""
    out = {}
    for path in source_files(root):
        with open(path, encoding='utf-8') as handle:
            tree = ast.parse(handle.read(), filename=path)
        visitor = _Visitor()
        visitor.visit(tree)
        module = os.path.relpath(path, root).replace(os.sep, '/')
        for field, count in visitor.found.items():
            out.setdefault(field, {})[module] = count
    return {field: dict(sorted(out[field].items()))
            for field in FIELDS if field in out}


def ratchet(baseline, current):
    """``(grown, shrunk)``: lines describing writes added since the baseline
    and writes removed (the baseline should then be lowered)."""
    grown, shrunk = [], []
    for field in FIELDS:
        before = baseline.get(field, {})
        after = current.get(field, {})
        for module in sorted(set(before) | set(after)):
            was, now = before.get(module, 0), after.get(module, 0)
            if now > was:
                grown.append('%s: %r writes %d -> %d' % (module, field, was, now))
            elif now < was:
                shrunk.append('%s: %r writes %d -> %d' % (module, field, was, now))
    return grown, shrunk
