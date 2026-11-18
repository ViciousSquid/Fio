"""Emissive decals: a decal that glows is a row of the frame's light list.

Its light takes the main colour of its image (times its tint), the
intensity and radius it names, and is emitted from the decal's face. A decal
that is not emissive is not a light in any sense: no class bit, no row in the
light list, no image decoded and no position arithmetic.
"""
import io

import numpy as np
import pytest

from editor.things import Decal, Light
from engine import decal_textures, entity_table as et

# The Decal entity is editor.things', which imports PyQt5 at module level:
# the module belongs to the Qt tier (the no-Qt jobs cannot import it).
pytestmark = pytest.mark.qt


def _png(colour, size=(8, 8), transparent_half=False):
    from PIL import Image
    image = Image.new("RGBA", size, tuple(colour) + (255,))
    if transparent_half:
        # The other half is a loud colour that is fully transparent: it must
        # not reach the average.
        for x in range(size[0] // 2):
            for y in range(size[1]):
                image.putpixel((x, y), (0, 255, 0, 0))
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def images():
    """Decal images served from memory, with a count of how often each is read."""
    reads = {}
    files = {
        "red.png": _png((255, 0, 0)),
        "blue_half.png": _png((0, 0, 255), transparent_half=True),
        "grey.png": _png((128, 128, 128)),
    }

    def reader(name):
        def read():
            reads[name] = reads.get(name, 0) + 1
            return files[name]
        return read

    decal_textures._average.clear()
    decal_textures.use_source(decal_textures.DecalSource(
        {name: reader(name) for name in files}))
    yield reads
    decal_textures.use_source(None)
    decal_textures._average.clear()


def _decal(ident, pos=(0.0, 0.0, 0.0), **props):
    props.setdefault("texture", "red.png")
    return Decal(pos=list(pos), properties=dict(props, id=ident))


def _table(things):
    table = et.EntityTable()
    table.sync(things, epoch=1)
    return table


def _light_ids(table):
    return [table.ids[slot] for slot in table.light_slots.tolist()]


# ---------------------------------------------------------------------------
# The default: not a light, and nothing paid for it
# ---------------------------------------------------------------------------

def test_a_decal_is_not_emissive_by_default():
    decal = Decal(pos=[0, 0, 0], properties={"id": "d"})

    assert decal.properties["emissive"] is False
    assert decal.properties["emissive_intensity"] == 1.0
    assert decal.properties["emissive_radius"] == 256.0


def test_a_plain_decal_costs_nothing(images):
    lamp = Light(pos=[0, 64, 0], properties={"id": "lamp"})
    table = _table([_decal("d1"), lamp, _decal("d2", pos=(64, 0, 0))])

    assert _light_ids(table) == ["lamp"], "a decal that does not glow is in the light list"
    assert not table._light_offsets, "the position writes would do offset work"
    assert not images, f"an image was decoded for a decal that does not glow: {images}"
    for slot in (0, 2):
        assert not table.class_bits[slot] & et.ENT_LIGHT
        assert not table.light_enabled[slot]
        assert tuple(table.pos[slot]) == tuple(table.things[slot].pos)


# ---------------------------------------------------------------------------
# Emissive: colour, amount and place
# ---------------------------------------------------------------------------

def test_an_emissive_decal_is_a_light_in_its_images_colour(images):
    table = _table([_decal("sign", emissive=True, emissive_intensity=2.5,
                           emissive_radius=300.0)])

    assert _light_ids(table) == ["sign"]
    assert table.light_enabled[0] and not table.light_casts_shadows[0]
    np.testing.assert_allclose(table.light_color[0], (1.0, 0.0, 0.0), atol=1e-3)
    np.testing.assert_allclose(table.light_params[0], (2.5, 300.0))


def test_transparent_pixels_do_not_colour_the_light(images):
    table = _table([_decal("sign", texture="blue_half.png", emissive=True)])

    np.testing.assert_allclose(table.light_color[0], (0.0, 0.0, 1.0), atol=0.02,
                               err_msg="the transparent half's colour leaked in")


def test_the_light_is_the_images_main_colour_not_its_average(images):
    """A yellow sign with black lettering lights yellow: an average would be
    a darker, muddier yellow."""
    from PIL import Image
    sign = Image.new("RGBA", (64, 64), (255, 230, 0, 255))
    for x in range(64):
        for y in range(40, 64):                 # more than a third is black
            sign.putpixel((x, y), (0, 0, 0, 255))
    out = io.BytesIO()
    sign.save(out, "PNG")
    decal_textures.use_source(decal_textures.DecalSource({"sign.png": out.getvalue}))

    table = _table([_decal("sign", texture="sign.png", emissive=True)])

    np.testing.assert_allclose(table.light_color[0], (1.0, 230 / 255.0, 0.0), atol=0.01)


def test_an_emissive_decal_is_flagged_for_its_own_draw():
    from engine.decal_table import EMISSIVE_FLAG, placed_row

    props = dict(texture="red.png", width=64.0, height=64.0, depth=32.0)
    plain = placed_row((0, 0, 0), props, 0)[1]
    glowing = placed_row((0, 0, 0), dict(props, emissive=True, mirrored=True), 0)[1]
    wet = placed_row((0, 0, 0), dict(props, texture="puddle.png", emissive=True), 0)[1]

    assert plain[3] == 0.0
    assert glowing[3] == EMISSIVE_FLAG + 1.0
    assert wet[3] < EMISSIVE_FLAG, "a wet patch is drawn by the wet pass, emissive or not"


def test_the_tint_multiplies_the_light(images):
    table = _table([_decal("sign", texture="grey.png", emissive=True,
                           tint=[1.0, 0.5, 0.0])])

    grey = 128 / 255.0
    np.testing.assert_allclose(table.light_color[0], (grey, grey * 0.5, 0.0), atol=0.01)


def test_a_white_decal_with_a_green_tint_emits_exactly_that_green(images):
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGBA", (16, 16), (255, 255, 255, 255)).save(out, "PNG")
    decal_textures.use_source(decal_textures.DecalSource({"white.png": out.getvalue}))
    green = [0.1, 0.9, 0.25]

    table = _table([_decal("panel", texture="white.png", emissive=True, tint=green)])

    np.testing.assert_allclose(table.light_color[0], green, atol=1e-6)


def test_a_decal_with_no_image_glows_its_tint(images):
    table = _table([_decal("words", texture="", text="EXIT", emissive=True,
                           tint=[0.0, 1.0, 0.0])])

    np.testing.assert_allclose(table.light_color[0], (0.0, 1.0, 0.0), atol=1e-6)


@pytest.mark.parametrize("yaw,pitch,expected", [
    (0.0, -90.0, (0.0, 1.0, 0.0)),       # thrown down onto a floor: light above it
    (0.0, 0.0, (-1.0, 0.0, 0.0)),        # thrown along +X onto a wall: light at -X
    (90.0, 0.0, (0.0, 0.0, 1.0)),        # thrown along -Z: light at +Z
])
def test_the_light_is_emitted_from_the_decals_face(images, yaw, pitch, expected):
    """On the surface the decal is painted on (its position), a unit in
    front: whatever the box's depth."""
    centre = (100.0, 50.0, -20.0)
    table = _table([_decal("sign", pos=centre, emissive=True, depth=32.0,
                           yaw=yaw, pitch=pitch)])

    np.testing.assert_allclose(table.pos[0], np.add(centre, expected), atol=1e-6)


def test_a_moved_emissive_decal_keeps_its_light_at_the_face(images):
    decal = _decal("sign", emissive=True, depth=32.0, yaw=0.0, pitch=-90.0)
    lamp = Light(pos=[0, 64, 0], properties={"id": "lamp"})
    things = [decal, lamp]
    table = _table(things)

    decal.pos = [10.0, 20.0, 30.0]              # journalled, as the Decal tool's drag is
    table.begin_frame(things, 1)

    np.testing.assert_allclose(table.pos[0], (10.0, 21.0, 30.0), atol=1e-6)
    np.testing.assert_allclose(table.pos[1], (0.0, 64.0, 0.0), atol=1e-6,
                               err_msg="another row was offset")


def test_a_hidden_emissive_decal_does_not_glow(images):
    table = _table([_decal("sign", emissive=True, hidden=True)])

    assert not table.light_enabled[0]


def test_each_image_is_read_once_however_many_decals_glow(images):
    _table([_decal(f"d{i}", emissive=True) for i in range(6)])
    _table([_decal("again", emissive=True)])

    assert images == {"red.png": 1}


# ---------------------------------------------------------------------------
# Ticking and clearing the box
# ---------------------------------------------------------------------------

def test_ticking_emissive_joins_the_light_list_and_clearing_it_leaves(images):
    from engine.change_journal import touch

    decal = _decal("sign", depth=32.0, yaw=0.0, pitch=-90.0)
    things = [decal]
    table = _table(things)
    assert _light_ids(table) == []

    decal.properties["emissive"] = True
    touch(decal)                                # as an edit of the entity does
    table.begin_frame(things, 1)
    assert _light_ids(table) == ["sign"]
    np.testing.assert_allclose(table.pos[0], (0.0, 1.0, 0.0), atol=1e-6)

    decal.properties["emissive"] = False
    touch(decal)
    table.begin_frame(things, 1)
    assert _light_ids(table) == []
    assert not table.light_enabled[0]
    np.testing.assert_allclose(table.pos[0], (0.0, 0.0, 0.0), atol=1e-6,
                               err_msg="the row kept its light's offset")


def test_the_offset_travels_with_a_row_that_changes_slot(images):
    decal = _decal("sign", pos=(5.0, 0.0, 0.0), emissive=True, depth=32.0,
                   yaw=0.0, pitch=-90.0)
    table = _table([decal])
    lamp = Light(pos=[0, 64, 0], properties={"id": "lamp"})

    table.sync([lamp, decal], epoch=2)          # the decal is now row 1

    assert sorted(_light_ids(table)) == ["lamp", "sign"]
    np.testing.assert_allclose(table.pos[1], (5.0, 1.0, 0.0), atol=1e-6)
    np.testing.assert_allclose(table.pos[0], (0.0, 64.0, 0.0), atol=1e-6)


def test_nonsense_amounts_do_not_poison_the_light_list(images):
    table = _table([_decal("sign", emissive=True, emissive_intensity="lots",
                           emissive_radius=float("nan"), depth=float("inf"))])

    assert np.isfinite(table.light_params[0]).all()
    assert np.isfinite(table.pos[0]).all()


# ---------------------------------------------------------------------------
# It is still a decal
# ---------------------------------------------------------------------------

def test_an_emissive_decal_is_still_filtered_as_a_decal(images):
    from engine import view_filters

    plain = view_filters.entity_group(_decal("a"))
    glowing = view_filters.entity_group(_decal("b", emissive=True))

    assert glowing == plain != "lights"


def test_emissive_survives_a_map_round_trip(images):
    from editor.things import Thing

    decal = _decal("sign", emissive=True, emissive_intensity=3.0, emissive_radius=99.0)
    again = Thing.from_dict(decal.to_dict())

    assert isinstance(again, Decal)
    assert again.properties["emissive"] is True
    assert again.properties["emissive_intensity"] == 3.0
    assert again.properties["emissive_radius"] == 99.0


# ---------------------------------------------------------------------------
# The emissive decals' own texture array: their images at full resolution
# ---------------------------------------------------------------------------

def test_emissive_decals_are_drawn_from_their_images_full_resolution():
    """An emissive decal is its image, unmodulated: a 256-pixel layer
    magnified across a screen is fuzzy (the bug), so the emissive draw reads
    an array sized to hold its largest image (a power of two, 256 to 1024)."""
    from engine.renderer.forward.decals import (
        DECAL_TEXTURE_SIZE, EMISSIVE_TEXTURE_MAX, emissive_side)
    assert DECAL_TEXTURE_SIZE == 256 and EMISSIVE_TEXTURE_MAX == 1024
    assert emissive_side([]) == 256
    assert emissive_side([(64, 64)]) == 256
    assert emissive_side([(256, 256)]) == 256
    assert emissive_side([(512, 512)]) == 512
    assert emissive_side([(625, 491), (256, 256)]) == 1024
    assert emissive_side([(1024, 256)]) == 1024          # a wide banner: its width
    assert emissive_side([(4096, 4096)]) == 1024          # capped
