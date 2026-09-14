"""Head-less tests for a face's texture transform.

``editor.face_texture`` is the layer that lets the Surface Inspector treat a
box brush's tagged side and an angled brush's cut face as the same thing, so
these cover both storage shapes and the projection operations built on them.
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from editor import face_texture as ft  # noqa: E402
from engine import brush_geometry as bg  # noqa: E402


def make_box(pos=(0, 0, 0), size=(512, 128, 64)):
    return {
        'pos': list(pos),
        'size': list(size),
        'textures': {tag: 'tex_%s.png' % tag for tag in bg.FACE_TAGS},
    }


def wedge():
    """A clipped brush, so its cut face has no box tag."""
    brush = make_box(size=(128, 128, 128))
    assert bg.clip_brush(brush, [1.0, 1.0, 0.0], 0.0)
    return brush


def cut_key(brush):
    return next(k for k in ft.face_keys(brush) if k.startswith('#'))


# ---------------------------------------------------------------------------
# Face addressing
# ---------------------------------------------------------------------------

def test_a_box_brush_lists_its_six_sides():
    assert sorted(ft.face_keys(make_box())) == sorted(bg.FACE_TAGS)


def test_a_clipped_brush_lists_its_cut_face_too():
    brush = wedge()
    keys = ft.face_keys(brush)
    assert len([k for k in keys if k.startswith('#')]) == 1
    # The cut took the east and top sides off; the rest are still addressable.
    assert 'west' in keys and 'north' in keys


def test_a_box_face_is_not_a_cut_face():
    assert not ft.is_cut_face(make_box(), 'north')


def test_a_cut_face_is_recognised():
    brush = wedge()
    assert ft.is_cut_face(brush, cut_key(brush))
    assert not ft.is_cut_face(brush, 'west')


# ---------------------------------------------------------------------------
# Reading and writing, both storage shapes
# ---------------------------------------------------------------------------

def test_an_untouched_face_reads_as_defaults():
    transform = ft.get_transform(make_box(), 'north')
    assert transform['shift'] == ft.DEFAULT_SHIFT
    assert transform['scale'] == ft.DEFAULT_SCALE
    assert transform['angle'] == ft.DEFAULT_ANGLE
    assert transform['texture'] == 'tex_north.png'


def test_a_box_face_stores_its_transform_on_the_brush():
    brush = make_box()
    ft.set_transform(brush, 'north', shift=(0.25, -0.5), scale=(2.0, 3.0),
                     angle=45.0)
    assert brush['uv_shift']['north'] == [0.25, -0.5]
    assert brush['uv_scale']['north'] == [2.0, 3.0]
    assert brush['uv_angle']['north'] == 45.0
    assert ft.get_transform(brush, 'north')['scale'] == (2.0, 3.0)


def test_a_cut_face_stores_its_transform_on_its_plane():
    brush = wedge()
    key = cut_key(brush)
    ft.set_transform(brush, key, shift=(1.0, 2.0), scale=(4.0, 0.5), angle=90.0)
    plane = ft.face_plane(brush, key)
    assert plane['uv_shift'] == [1.0, 2.0]
    assert plane['uv_scale'] == [4.0, 0.5]
    assert plane['uv_angle'] == 90.0
    assert 'uv_shift' not in brush          # not smuggled into the brush dicts
    assert ft.get_transform(brush, key)['angle'] == 90.0


def test_writing_a_texture_to_a_tagged_face_of_an_angled_brush():
    """The plane draws the face, so it has to learn the new texture too."""
    brush = wedge()
    ft.set_transform(brush, 'west', texture='marble.png')
    assert brush['textures']['west'] == 'marble.png'
    assert ft.face_plane(brush, 'west')['texture'] == 'marble.png'


def test_writing_a_texture_to_a_cut_face():
    brush = wedge()
    key = cut_key(brush)
    ft.set_transform(brush, key, texture='slope.png')
    assert ft.face_plane(brush, key)['texture'] == 'slope.png'
    assert ft.get_transform(brush, key)['texture'] == 'slope.png'


def test_setting_one_field_leaves_the_others_alone():
    brush = make_box()
    ft.set_transform(brush, 'north', scale=(2.0, 2.0), angle=30.0)
    ft.set_transform(brush, 'north', shift=(0.5, 0.5))
    transform = ft.get_transform(brush, 'north')
    assert transform['scale'] == (2.0, 2.0)
    assert transform['angle'] == 30.0
    assert transform['shift'] == (0.5, 0.5)


# ---------------------------------------------------------------------------
# Face extent
# ---------------------------------------------------------------------------

def test_box_face_extents_follow_the_brush_dimensions():
    brush = make_box(size=(512, 128, 64))
    assert ft.face_extent(brush, 'north') == (512.0, 128.0)
    assert ft.face_extent(brush, 'east') == (64.0, 128.0)
    assert ft.face_extent(brush, 'top') == (512.0, 64.0)


def test_a_cut_face_is_measured_along_its_texture_axes():
    """Extent is measured in the projection the renderer actually uses.

    An unrotated cut face still projects on world axes, so a 45-degree slope
    across a 128 cube measures 128 — the projected span, not the slope's true
    length.  Natural has to agree with what is drawn, so it measures the same
    way; a face that has been rotated carries a basis tangent to itself and is
    then measured at true size.
    """
    brush = wedge()
    width, height = ft.face_extent(brush, cut_key(brush))
    assert width == pytest.approx(128.0)
    assert height == pytest.approx(128.0)


# ---------------------------------------------------------------------------
# Projections
# ---------------------------------------------------------------------------

def test_fit_spans_the_face_the_requested_number_of_times():
    brush = make_box()
    ft.apply_fit(brush, 'north', (1.0, 1.0))
    assert ft.get_transform(brush, 'north')['scale'] == (1.0, 1.0)
    ft.apply_fit(brush, 'north', (2.0, 3.0))
    assert ft.get_transform(brush, 'north')['scale'] == (2.0, 3.0)


def test_fit_clears_rotation_and_offset():
    brush = make_box()
    ft.set_transform(brush, 'north', shift=(0.3, 0.7), angle=33.0)
    ft.apply_fit(brush, 'north')
    transform = ft.get_transform(brush, 'north')
    assert transform['shift'] == (0.0, 0.0)
    assert transform['angle'] == 0.0


def test_fit_is_the_same_whatever_the_face_size():
    """Face UVs are normalised, so fitting needs no measuring."""
    small = make_box(size=(32, 32, 32))
    huge = make_box(size=(2048, 512, 64))
    ft.apply_fit(small, 'north', (1.0, 1.0))
    ft.apply_fit(huge, 'north', (1.0, 1.0))
    assert ft.get_transform(small, 'north')['scale'] == \
        ft.get_transform(huge, 'north')['scale']


def test_natural_gives_one_texel_per_world_unit():
    brush = make_box(size=(512, 128, 64))
    ft.apply_natural(brush, 'north', texture_size=(128, 128))
    assert ft.get_transform(brush, 'north')['scale'] == (4.0, 1.0)


def test_natural_accounts_for_the_texture_resolution():
    brush = make_box(size=(512, 128, 64))
    ft.apply_natural(brush, 'north', texture_size=(512, 512))
    assert ft.get_transform(brush, 'north')['scale'] == (1.0, 0.25)


def test_natural_keeps_a_face_looking_the_same_at_any_size():
    """Doubling a wall doubles the repeats, so the texels stay the same size."""
    small = make_box(size=(256, 128, 64))
    big = make_box(size=(512, 128, 64))
    ft.apply_natural(small, 'north', texture_size=(128, 128))
    ft.apply_natural(big, 'north', texture_size=(128, 128))
    assert ft.get_transform(big, 'north')['scale'][0] == \
        2 * ft.get_transform(small, 'north')['scale'][0]


def test_natural_works_on_a_cut_face():
    brush = wedge()
    key = cut_key(brush)
    ft.apply_natural(brush, key, texture_size=(128, 128))
    width, _ = ft.face_extent(brush, key)
    assert ft.get_transform(brush, key)['scale'][0] == pytest.approx(width / 128.0)


def test_axial_drops_a_rotated_face_back_onto_the_world_axes():
    brush = make_box()
    bg.box_to_geometry(brush)
    bg.rotate_brush(brush, 40.0, [0.0, 1.0, 0.0])
    plane = ft.face_plane(brush, 'north')
    assert plane.get('uv_u') is not None        # locked by the rotation
    ft.apply_axial(brush, 'north', texture_size=(128, 128))
    assert ft.face_plane(brush, 'north').get('uv_u') is None
    assert ft.get_transform(brush, 'north')['angle'] == 0.0


def test_axial_leaves_other_faces_locked():
    brush = make_box()
    bg.box_to_geometry(brush)
    bg.rotate_brush(brush, 40.0, [0.0, 1.0, 0.0])
    ft.apply_axial(brush, 'north', texture_size=(128, 128))
    assert ft.face_plane(brush, 'south').get('uv_u') is not None


# ---------------------------------------------------------------------------
# Flip and grid snapping
# ---------------------------------------------------------------------------

def test_flip_horizontal_negates_the_horizontal_scale():
    brush = make_box()
    ft.set_transform(brush, 'north', scale=(2.0, 3.0))
    ft.flip(brush, 'north', horizontal=True)
    assert ft.get_transform(brush, 'north')['scale'] == (-2.0, 3.0)


def test_flipping_twice_restores_the_original():
    brush = make_box()
    ft.set_transform(brush, 'north', scale=(2.0, 3.0))
    ft.flip(brush, 'north', vertical=True)
    ft.flip(brush, 'north', vertical=True)
    assert ft.get_transform(brush, 'north')['scale'] == (2.0, 3.0)


def test_match_grid_snaps_every_value_to_its_own_step():
    brush = make_box()
    ft.set_transform(brush, 'north', shift=(0.31, -0.2), scale=(2.3, 0.9),
                     angle=43.0)
    ft.match_grid(brush, 'north', {'shift': (0.125, 0.125),
                                   'scale': (0.5, 0.5),
                                   'angle': 45.0})
    transform = ft.get_transform(brush, 'north')
    assert transform['shift'] == (0.25, -0.25)      # 0.31 -> 2 steps of 0.125
    assert transform['scale'] == (2.5, 1.0)
    assert transform['angle'] == 45.0


def test_a_zero_step_leaves_its_value_alone():
    brush = make_box()
    ft.set_transform(brush, 'north', shift=(0.31, 0.0), angle=43.0)
    ft.match_grid(brush, 'north', {'shift': (0.0, 0.0), 'scale': (0.0, 0.0),
                                   'angle': 0.0})
    assert ft.get_transform(brush, 'north')['shift'][0] == 0.31
    assert ft.get_transform(brush, 'north')['angle'] == 43.0


def test_match_grid_works_on_a_cut_face():
    brush = wedge()
    key = cut_key(brush)
    ft.set_transform(brush, key, scale=(2.3, 2.3), angle=43.0)
    ft.match_grid(brush, key, {'shift': (0.125, 0.125), 'scale': (0.5, 0.5),
                               'angle': 45.0})
    assert ft.get_transform(brush, key)['scale'] == (2.5, 2.5)
