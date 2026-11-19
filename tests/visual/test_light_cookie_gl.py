"""Visual tier: a Light's cookie projects its image through 360 degrees.

A Light with ``cookie_enabled`` and a ``cookie_texture`` lights the room with
its image's own colours, read along the direction from the light to each
fragment (an octahedral map: engine.light_cookies). These render a closed
room around one light and check what reaches the frame:

* an ordinary light -- and one whose cookie is off, or names no image -- is
  drawn *bit for bit* as by shaders built without the cookie code at all;
* the image is recognisable, the right way round, on the floor, the ceiling
  and all four walls;
* it stretches with distance, as a projection does;
* it softens towards the light's radius, through mip levels;
* the light's own shadow darkens it to the floor, never to black.
"""

import configparser
import io

import numpy as np
import pytest

from tests.helpers import gl as glh

pytestmark = [pytest.mark.gl, pytest.mark.slow]

SIZE = 128
ROOM = 512.0            # half the room's width; floor y=0, ceiling y=2*ROOM... see _room
CEILING = 512.0
LIGHT = (0.0, 256.0, 0.0)

QUADRANTS = 'assets/textures/cookies/test_quadrants.png'
DISC = 'assets/textures/cookies/test_disc.png'
CHECKER = 'assets/textures/cookies/test_checker.png'
MISSING = 'assets/textures/cookies/no_such_image.png'

RED, GREEN, BLUE, YELLOW = (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0)


def _png(image):
    data = io.BytesIO()
    image.save(data, format='PNG')
    return data.getvalue()


def _quadrants():
    """Image top-left red, top-right green, bottom-left blue, bottom-right
    yellow: with the octahedral layout, every direction keeps its horizontal
    quadrant -- (-X,-Z) red, (+X,-Z) green, (-X,+Z) blue, (+X,+Z) yellow --
    on the floor, the walls and the (folded) ceiling alike."""
    from PIL import Image, ImageDraw
    image = Image.new('RGB', (256, 256))
    draw = ImageDraw.Draw(image)
    for colour, box in ((RED, (0, 0, 127, 127)), (GREEN, (128, 0, 255, 127)),
                        (BLUE, (0, 128, 127, 255)), (YELLOW, (128, 128, 255, 255))):
        draw.rectangle(box, fill=colour)
    return _png(image)


DISC_RHO = 0.3          # the disc's radius in octahedral units (centre = down)


def _disc():
    from PIL import Image, ImageDraw
    n = 512
    image = Image.new('RGB', (n, n))
    r = DISC_RHO * n / 2
    ImageDraw.Draw(image).ellipse((n / 2 - r, n / 2 - r, n / 2 + r, n / 2 + r),
                                  fill=(255, 255, 255))
    return _png(image)


def _checker():
    cells = (np.indices((512, 512)) // 4).sum(axis=0) % 2
    from PIL import Image
    return _png(Image.fromarray((cells * 255).astype(np.uint8)).convert('RGB'))


IMAGES = {}


@pytest.fixture
def images(monkeypatch):
    from engine import light_cookies
    IMAGES.update({QUADRANTS: _quadrants(), DISC: _disc(), CHECKER: _checker()})
    monkeypatch.setattr(light_cookies, 'project_bytes', lambda path: IMAGES.get(path))
    return IMAGES


def _renderer(lowpower=False):
    config = configparser.ConfigParser()
    config['Renderer'] = {'lowpower_mode': 'true' if lowpower else 'false'}
    return glh.make_renderer(config)


@pytest.fixture
def scene(images):
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        made = _renderer()
        try:
            yield made, context
        finally:
            made.cleanup()
    glh.reset_texture_cache()


def _room(floor_only=False, width=None):
    from tests.helpers.worlds import box_brush
    w = width or 2 * ROOM + 64
    brushes = [box_brush('floor', (0, -16, 0), (w, 32, w))]
    if not floor_only:
        brushes += [
            box_brush('ceiling', (0, CEILING + 16, 0), (w, 32, w)),
            box_brush('east', (ROOM + 16, CEILING / 2, 0), (32, CEILING + 64, w)),
            box_brush('west', (-ROOM - 16, CEILING / 2, 0), (32, CEILING + 64, w)),
            box_brush('north', (0, CEILING / 2, -ROOM - 16), (w, CEILING + 64, 32)),
            box_brush('south', (0, CEILING / 2, ROOM + 16), (w, CEILING + 64, 32)),
        ]
    return brushes


def _lamp(pos=LIGHT, radius=1400.0, intensity=2.0, **props):
    from editor.things import Light
    from tests.helpers.worlds import make_thing
    props.setdefault('casts_shadows', False)
    return make_thing(Light, 'lamp', pos, radius=radius, intensity=intensity,
                      state='on', **props)


def _matrices(eye, forward, up, fov=90.0):
    import glm
    eye = glm.vec3(*eye)
    view = glm.lookAt(eye, eye + glm.vec3(*forward), glm.vec3(*up))
    return glm.perspective(glm.radians(fov), 1.0, 1.0, 6000.0), view, eye


#: Six views from the light's own position: down, up and the four walls.
VIEWS = {
    'floor': ((0, -1, 0), (0, 0, -1)),
    'ceiling': ((0, 1, 0), (0, 0, 1)),
    'east': ((1, 0, 0), (0, 1, 0)),
    'west': ((-1, 0, 0), (0, 1, 0)),
    'north': ((0, 0, -1), (0, 1, 0)),
    'south': ((0, 0, 1), (0, 1, 0)),
}


def _frame(made, context, brushes, lights, matrices):
    projection, view, eye = matrices
    config = glh.render_config(all_brushes=brushes, all_things=lights,
                               all_lights=lights, play_mode=True)
    context.bind()
    with glh.no_gl_errors("cookie light"):
        made.render_scene(projection, view, eye, None, config,
                          brush_slots=config["all_brush_slots"])
    return context.read_pixels().astype(np.int32)


def _pixel(matrices, point):
    import glm
    projection, view, _ = matrices
    clip = projection * view * glm.vec4(*point, 1.0)
    ndc = glm.vec3(clip) / clip.w
    x = int(round((ndc.x * 0.5 + 0.5) * (SIZE - 1)))
    y = int(round((0.5 - ndc.y * 0.5) * (SIZE - 1)))
    assert 2 <= x < SIZE - 2 and 2 <= y < SIZE - 2, (point, x, y)
    return x, y


def _colour_at(image, matrices, point):
    x, y = _pixel(matrices, point)
    return image[y - 1:y + 2, x - 1:x + 2].reshape(-1, 3).mean(axis=0)


def _quadrant_colour(x, z):
    return {(False, False): RED, (True, False): GREEN,
            (False, True): BLUE, (True, True): YELLOW}[(x > 0, z > 0)]


def _matches(colour, expected):
    """Lit by *expected* and nothing else: its channels lead, the others
    stay near the ambient floor."""
    on = [c for c, e in zip(colour, expected) if e]
    off = [c for c, e in zip(colour, expected) if not e]
    return min(on) > 60 and (not off or max(off) < 0.45 * min(on))


def _surface_points(surface):
    """Points well inside each horizontal quadrant of one surface."""
    a, h = 200.0, CEILING / 2
    if surface == 'floor':
        return [(x, 0.0, z) for x in (-a, a) for z in (-a, a)]
    if surface == 'ceiling':
        return [(x, CEILING, z) for x in (-a, a) for z in (-a, a)]
    if surface in ('east', 'west'):
        x = ROOM if surface == 'east' else -ROOM
        return [(x, h + dy, z) for z in (-a, a) for dy in (-120.0, 120.0)]
    z = -ROOM if surface == 'north' else ROOM
    return [(x, h + dy, z) for x in (-a, a) for dy in (-120.0, 120.0)]


# ---------------------------------------------------------------------------
# Disabled means identical
# ---------------------------------------------------------------------------

def test_an_ordinary_light_is_drawn_exactly_as_without_the_feature(images, monkeypatch):
    """Shaders built with the cookie code, and lights whose cookie is off or
    names no readable image, give the very same pixels as shaders built
    without any of it -- the original lighting path."""
    import re
    from engine import shaders
    room = _room()
    variants = {
        'plain': _lamp(),
        'cookie off': _lamp(cookie_enabled=False, cookie_texture=QUADRANTS),
        'no image': _lamp(cookie_enabled=True, cookie_texture=MISSING),
        'no path': _lamp(cookie_enabled=True, cookie_texture=''),
    }
    for lowpower in (False, True):
        frames = {}
        glh.reset_texture_cache()
        with glh.GLTestContext(SIZE, SIZE) as context:
            # The reference: light_ubo_source never splices the cookie in.
            with monkeypatch.context() as patch:
                patch.setattr(shaders, '_MAIN_RE', re.compile(r'(?!)'))
                made = _renderer(lowpower)
                try:
                    frames['reference'] = [
                        _frame(made, context, room, [_lamp()],
                               _matrices(LIGHT, *VIEWS[v])) for v in VIEWS]
                finally:
                    made.cleanup()
            made = _renderer(lowpower)
            try:
                for name, lamp in variants.items():
                    frames[name] = [_frame(made, context, room, [lamp],
                                           _matrices(LIGHT, *VIEWS[v])) for v in VIEWS]
                # A missing image is looked for once; nothing is uploaded.
                cookies = made._light_cookies
                assert cookies is None or cookies.texture is None, (
                    "a texture was made for no image")
            finally:
                made.cleanup()
        glh.reset_texture_cache()
        for name in variants:
            for view, got, want in zip(VIEWS, frames[name], frames['reference']):
                assert np.array_equal(got, want), (
                    f"{name} ({'low-power' if lowpower else 'desktop'}) differs "
                    f"on the {view}: max {np.abs(got - want).max()}")


# ---------------------------------------------------------------------------
# Recognisable, the right way round, everywhere
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('lowpower', [False, True], ids=['desktop', 'low-power'])
def test_the_image_is_projected_the_right_way_round_on_every_surface(images, lowpower):
    room = _room()
    lamp = _lamp(cookie_enabled=True, cookie_texture=QUADRANTS)
    glh.reset_texture_cache()
    with glh.GLTestContext(SIZE, SIZE) as context:
        made = _renderer(lowpower)
        try:
            for surface, (forward, up) in VIEWS.items():
                matrices = _matrices(LIGHT, forward, up)
                image = _frame(made, context, room, [lamp], matrices)
                for point in _surface_points(surface):
                    colour = _colour_at(image, matrices, point)
                    expected = _quadrant_colour(point[0], point[2])
                    assert _matches(colour, expected), (
                        surface, point, colour.round(), expected)
        finally:
            made.cleanup()
    glh.reset_texture_cache()


def test_only_the_cookie_light_is_coloured(scene):
    """A second, ordinary light beside it lights white, as before."""
    made, context = scene
    room = _room(floor_only=True)
    plain = _lamp((0.0, 256.0, 0.0), radius=700.0)
    plain.properties['name'] = 'plain'
    matrices = _matrices((0.0, 400.0, 0.0), (0, -1, 0), (0, 0, -1))
    image = _frame(made, context, room, [plain], matrices)
    colour = _colour_at(image, matrices, (-150.0, 0.0, -150.0))
    assert abs(colour[0] - colour[1]) < 3 and abs(colour[1] - colour[2]) < 3, colour


# ---------------------------------------------------------------------------
# Stretching and softening
# ---------------------------------------------------------------------------

def _lit_radius(image, matrices, height):
    """The world radius of the lit disc on the floor, along +X."""
    lum = glh.luminance(image)
    steps = np.arange(0.0, 300.0, 2.0)
    values = np.array([lum[_pixel(matrices, (x, 0.0, 0.0))[1],
                           _pixel(matrices, (x, 0.0, 0.0))[0]] for x in steps])
    inside = values > (values[0] + values.min()) / 2
    return float(steps[np.argmin(inside)])


def test_the_image_stretches_with_distance(scene):
    """Twice as far from the floor, the same picture covers twice the width:
    it is a projection, not a decal."""
    made, context = scene
    room = _room(floor_only=True)
    matrices = _matrices((0.0, 900.0, 0.0), (0, -1, 0), (0, 0, -1), fov=40.0)
    radii = {}
    for height in (100.0, 200.0):
        lamp = _lamp((0.0, height, 0.0), radius=3000.0,
                     cookie_enabled=True, cookie_texture=DISC)
        radii[height] = _lit_radius(_frame(made, context, room, [lamp], matrices),
                                    matrices, height)
        # Where the octahedral map puts the disc's edge: x / (x + h) = rho.
        expected = DISC_RHO * height / (1.0 - DISC_RHO)
        assert abs(radii[height] - expected) < 0.12 * expected + 4.0, (height, radii)
    assert 1.8 < radii[200.0] / radii[100.0] < 2.2, radii


def _contrast(image):
    lum = glh.luminance(image)[SIZE // 4:3 * SIZE // 4, SIZE // 4:3 * SIZE // 4]
    return float(lum.std() / max(lum.mean(), 1e-6))


def _checker_contrasts(made, context):
    """Checker contrast on the floor straight below the light, the light
    raised step by step towards its radius. The receiving surface faces the
    light square on every time, and the camera is moved in so a texel covers
    the same few pixels each time (the screen-footprint level stays put):
    what changes is the distance softening alone."""
    room = _room(floor_only=True, width=4000.0)
    radius = 900.0
    contrasts = []
    for height in (150.0, 400.0, 650.0, 850.0):
        # As bright below the light each time: the falloff is undone, so
        # 8-bit quantisation does not flatten the far checker on its own.
        falloff = (1.0 - height / radius) ** 2
        lamp = _lamp((0.0, height, 0.0), radius=radius, intensity=0.8 / falloff,
                     cookie_enabled=True, cookie_texture=CHECKER)
        texel = 2.0 * height / 512          # centre of the octahedral map
        span = texel * SIZE / 4.0
        camera = span / 2.0 / np.tan(np.radians(15.0))
        matrices = _matrices((1.0, camera, 1.0), (0, -1, 0), (0, 0, -1), fov=30.0)
        contrasts.append(_contrast(_frame(made, context, room, [lamp], matrices)))
    return contrasts


def test_the_image_softens_towards_the_radius(images, monkeypatch):
    from engine import shaders
    measured = {}
    for name, glsl in (('soft', shaders.LIGHT_COOKIE_GLSL),
                       ('sharp', shaders.LIGHT_COOKIE_GLSL.replace(
                           repr(shaders.COOKIE_SOFTEN_LODS) + ' * clamp', '0.0 * clamp'))):
        assert name == 'soft' or glsl != shaders.LIGHT_COOKIE_GLSL
        monkeypatch.setattr(shaders, 'LIGHT_COOKIE_GLSL', glsl)
        glh.reset_texture_cache()
        with glh.GLTestContext(SIZE, SIZE) as context:
            made = _renderer()
            try:
                measured[name] = _checker_contrasts(made, context)
            finally:
                made.cleanup()
        glh.reset_texture_cache()
    soft, sharp = measured['soft'], measured['sharp']
    # Without the bias the checker stays crisp at every distance...
    assert min(sharp) > 0.5 * max(sharp), measured
    # ...with it, it fades progressively, still recognisable near the light.
    assert all(a > b for a, b in zip(soft, soft[1:])), measured
    assert soft[0] > 0.5 * sharp[0], measured
    assert soft[-1] < 0.35 * sharp[-1], measured


# ---------------------------------------------------------------------------
# Shadows: independent, and the light's own shadow only dims the image
# ---------------------------------------------------------------------------

def _shadow_scene(made, context, **props):
    """A pillar between the light and part of the floor."""
    from tests.helpers.worlds import box_brush
    room = _room(floor_only=True) + [box_brush('pillar', (-150, 60, -150), (60, 120, 60))]
    lamp = _lamp((0.0, 200.0, 0.0), radius=900.0, intensity=3.0, **props)
    matrices = _matrices((0.0, 700.0, 0.0), (0, -1, 0), (0, 0, -1), fov=60.0)
    image = _frame(made, context, room, [lamp], matrices)
    # Behind the pillar, along the ray from the light, and an open spot
    # mirrored across the light.
    behind = _colour_at(image, matrices, (-330.0, 0.0, -330.0))
    open_ = _colour_at(image, matrices, (-330.0, 0.0, 330.0))
    return behind, open_


def test_a_cookie_light_that_casts_shadows_keeps_its_image_in_its_shadow(scene):
    made, context = scene
    behind, open_ = _shadow_scene(
        made, context, casts_shadows=True, cookie_enabled=True,
        cookie_texture=QUADRANTS)
    # (-X,-Z) is red, (-X,+Z) blue: the shadowed spot is still red, dimmer.
    assert behind[0] > behind[1] + 8 and behind[0] > behind[2] + 8, behind
    assert _matches(open_, BLUE), open_
    assert behind[0] < 0.6 * open_[2], (behind, open_)


def test_a_cookie_light_without_shadows_lights_through_geometry(scene):
    made, context = scene
    behind, open_ = _shadow_scene(made, context, cookie_enabled=True,
                                  cookie_texture=QUADRANTS)
    assert _matches(behind, RED), behind


def test_casting_shadows_stays_independent_of_the_cookie():
    """Both flags reach the projection; neither clears the other."""
    from editor.things import Light
    from engine.entity_table import EntityTable
    from engine.light_cookies import intern_cookie
    lamp = Light(pos=[0, 0, 0], properties={
        'casts_shadows': True, 'cookie_enabled': True, 'cookie_texture': QUADRANTS,
        'cookie_shadow_floor': 0.4})
    table = EntityTable()
    table.begin_frame([lamp], epoch=1)
    assert table.light_casts_shadows[0]
    assert table.light_cookie[0] == intern_cookie(QUADRANTS)
    assert table.light_cookie_floor[0] == pytest.approx(0.4)


# ---------------------------------------------------------------------------
# The cookie unit always holds a complete array
# ---------------------------------------------------------------------------

def _cookie_unit_texture():
    from OpenGL import GL as gl
    from engine.shaders import COOKIE_TEXTURE_UNIT
    gl.glActiveTexture(gl.GL_TEXTURE0 + COOKIE_TEXTURE_UNIT)
    try:
        texture = int(gl.glGetIntegerv(gl.GL_TEXTURE_BINDING_2D_ARRAY))
        complete = None
        if texture:
            width = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D_ARRAY, 0, gl.GL_TEXTURE_WIDTH)
            complete = int(width) > 0
        return texture, complete
    finally:
        gl.glActiveTexture(gl.GL_TEXTURE0)


def test_the_cookie_sampler_has_a_complete_array_with_no_cookie_light(scene):
    """Every lit program declares the cookie sampler. With no cookie light
    in view a complete 1x1 array stands on its unit, so a driver that checks
    every active sampler (macOS: "GLD_TEXTURE_INDEX_2D_ARRAY is unloadable")
    finds one; with a cookie light, the cookie array is there instead."""
    made, context = scene
    matrices = _matrices((0.0, 400.0, 0.0), (0, -1, 0), (0, 0, -1))
    _frame(made, context, _room(floor_only=True), [_lamp((0.0, 256.0, 0.0))], matrices)
    placeholder, complete = _cookie_unit_texture()
    assert placeholder and complete, "no complete array on the cookie unit"

    cookie = _lamp((0.0, 256.0, 0.0), cookie_enabled=True, cookie_texture=QUADRANTS)
    _frame(made, context, _room(floor_only=True), [cookie], matrices)
    texture, complete = _cookie_unit_texture()
    assert texture and complete and texture != placeholder

    _frame(made, context, _room(floor_only=True), [_lamp((0.0, 256.0, 0.0))], matrices)
    assert _cookie_unit_texture()[0] == placeholder, "back to the placeholder"
