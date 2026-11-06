"""A uniform location is valid from 0; only -1 means "not in the program".

Apple's OpenGL put ``normalMatrix`` at location 0, and five draw paths
checked ``normal_mat_loc > 0``: every angled (convex) brush and water face
then drew with a zero normal matrix, so no light reached them on macOS --
the blue ORB lit the ceiling (a box brush) but not the floor (an angled
brush). Mesa assigns other locations, so Linux and Windows never showed it.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [*sorted((ROOT / 'engine').rglob('*.py')), *sorted((ROOT / 'editor').rglob('*.py'))]


def _names_location(node):
    name = node.id if isinstance(node, ast.Name) else getattr(node, 'attr', '')
    if isinstance(node, ast.Subscript):
        key = node.slice
        name = key.value if isinstance(key, ast.Constant) and isinstance(key.value, str) else ''
        # uniforms['normalMatrix'] style lookups are locations too.
        return bool(name) and isinstance(node.value, (ast.Name, ast.Attribute)) and \
            'uniform' in ast.unparse(node.value).lower()
    return name.endswith(('_loc', '_location')) or name in ('loc', 'location')


def _bad_comparisons(tree):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare) or len(node.ops) != 1:
            continue
        left, op, right = node.left, node.ops[0], node.comparators[0]
        if not (_names_location(left) and isinstance(right, ast.Constant)):
            continue
        if (isinstance(op, ast.Gt) and right.value == 0) or (isinstance(op, ast.GtE) and right.value == 1):
            yield node.lineno, ast.unparse(node)


def test_no_uniform_location_is_treated_as_missing_at_zero():
    found = []
    for path in SOURCES:
        tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
        found += [f'{path.relative_to(ROOT)}:{line}: {text}' for line, text in _bad_comparisons(tree)]
    assert not found, 'uniform location 0 is valid; compare with != -1:\n' + '\n'.join(found)


def test_the_check_recognises_the_original_mistake():
    tree = ast.parse('if normal_mat_loc > 0:\n    pass\nif self.u_loc >= 1:\n    pass\n'
                     'if normal_mat_loc != -1:\n    pass\n')
    assert [text for _, text in _bad_comparisons(tree)] == ['normal_mat_loc > 0', 'self.u_loc >= 1']
