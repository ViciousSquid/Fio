"""HD companion maps: the generator, the runtime cache and the stock assets.

``engine.material_maps`` derives a normal map and a material map from a colour
image; ``tools/make_material_maps.py`` bakes them beside the stock textures.
Headless: NumPy and Pillow only.
"""

import io
import json
import os

import numpy as np
import pytest
from PIL import Image

from engine import material_maps as mm

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
TEXTURES = os.path.join(ROOT, 'assets', 'textures')


def _pattern(rows=64, cols=96):
    """A tiling colour image with broad shapes, fine grain and a bright spot."""
    y, x = np.mgrid[0:rows, 0:cols]
    wave = 0.5 + 0.25 * np.sin(2 * np.pi * x / cols) * np.cos(4 * np.pi * y / rows)
    grain = ((x * 7 + y * 13) % 11) / 44.0
    image = np.empty((rows, cols, 3), np.uint8)
    image[:, :, 0] = np.clip((wave + grain) * 200, 0, 255)
    image[:, :, 1] = np.clip((wave - grain) * 220, 0, 255)
    image[:, :, 2] = np.clip(wave * 180, 0, 255)
    image[10:14, 20:30] = 255
    return image


def _png_bytes(array):
    buffer = io.BytesIO()
    Image.fromarray(array).save(buffer, format='PNG')
    return buffer.getvalue()


def _stock(folder=TEXTURES):
    """(rel, path) of every file under assets/textures."""
    found = []
    for base, dirs, files in os.walk(folder):
        dirs.sort()
        for name in sorted(files):
            path = os.path.join(base, name)
            found.append((os.path.relpath(path, folder).replace(os.sep, '/'), path))
    return found


# -- the generator -------------------------------------------------------------

def test_generate_is_deterministic():
    image = _pattern()
    first = mm.generate(image, {'emission_threshold': 0.9})
    second = mm.generate(image.copy(), {'emission_threshold': 0.9})
    assert np.array_equal(first[0], second[0])
    assert np.array_equal(first[1], second[1])


def test_generate_returns_rgba_bytes_of_the_input_size():
    image = _pattern(40, 72)
    for source in (image, np.dstack([image, np.full(image.shape[:2], 255, np.uint8)])):
        normal, material = mm.generate(source)
        assert normal.shape == material.shape == (40, 72, 4)
        assert normal.dtype == material.dtype == np.uint8


def test_rgb_and_opaque_rgba_give_the_same_maps():
    image = _pattern()
    opaque = np.dstack([image, np.full(image.shape[:2], 255, np.uint8)])
    for a, b in zip(mm.generate(image), mm.generate(opaque)):
        assert np.array_equal(a, b)


@pytest.mark.parametrize('shift', [(5, 0), (0, -17), (23, 41)])
def test_generated_maps_tile(shift):
    """Every filter wraps: shifting the input shifts the output, exactly."""
    image = _pattern()
    params = {'emission_threshold': 0.9, 'metal': 0.5}
    normal, material = mm.generate(image, params)
    moved_normal, moved_material = mm.generate(np.roll(image, shift, (0, 1)), params)
    assert np.array_equal(np.roll(normal, shift, (0, 1)), moved_normal)
    assert np.array_equal(np.roll(material, shift, (0, 1)), moved_material)


def test_flat_input_gives_a_flat_normal_and_no_occlusion():
    flat = np.full((32, 48, 3), 90, np.uint8)
    normal, material = mm.generate(flat, {'metal': 1.0, 'roughness_base': 0.5})
    assert (normal[:, :, :3] == (128, 128, 255)).all()
    assert (normal[:, :, 3] == 255).all()            # nothing to push down
    assert (material[:, :, 0] == 255).all()          # no occlusion
    assert (material[:, :, 1] == 128).all()          # roughness_base
    assert (material[:, :, 2] == 255).all()          # metal
    assert (material[:, :, 3] == 255).all()          # not emissive


def test_normals_are_unit_length_and_face_outwards():
    normal, _material = mm.generate(_pattern(), {'height_strength': 2.0})
    vectors = (normal[:, :, :3].astype(np.float64) - 128.0) / 127.0
    lengths = np.linalg.norm(vectors, axis=2)
    assert np.all(np.abs(lengths - 1.0) < 0.03)
    assert np.all(vectors[:, :, 2] > 0.0)
    assert vectors[:, :, 2].mean() > 0.7
    assert normal[:, :, :2].std() > 0.5              # and it is not just flat


def test_normal_map_is_opengl_convention():
    """A bright ridge: its left side leans left (-X), its upper side up (+Y)."""
    image = np.full((64, 64, 3), 40, np.uint8)
    image[24:40, 24:40] = 220
    normal, _material = mm.generate(image, {'detail': 0.0, 'height_strength': 2.0})
    x = normal[:, :, 0].astype(int) - 128
    y = normal[:, :, 1].astype(int) - 128
    assert x[32, 22:26].mean() < -2 and x[32, 38:42].mean() > 2
    assert y[22:26, 32].mean() > 2 and y[38:42, 32].mean() < -2      # row 0 is the top
    assert normal[32, 32, 3] > normal[4, 4, 3]       # bright is high


def test_invert_makes_dark_high():
    image = np.full((64, 64, 3), 40, np.uint8)
    image[24:40, 24:40] = 220
    normal, _material = mm.generate(image, {'invert': True, 'detail': 0.0})
    assert normal[32, 32, 3] < normal[4, 4, 3]


def test_emission_threshold_marks_bright_texels():
    image = np.full((32, 32, 3), 60, np.uint8)
    image[8:12, 8:20] = (20, 40, 255)                # a saturated blue lamp
    _normal, dark = mm.generate(image)
    assert (dark[:, :, 3] == 255).all()
    _normal, material = mm.generate(image, {'emission_threshold': 0.8})
    emission = 255 - material[:, :, 3].astype(int)
    assert (emission[8:12, 8:20] == 255).all()
    lamp = np.zeros((32, 32), bool)
    lamp[8:12, 8:20] = True
    assert (emission[~lamp] == 0).all()


def test_emission_mask_overrides_the_threshold():
    image = np.full((16, 16, 3), 250, np.uint8)
    mask = np.zeros((16, 16), np.uint8)
    mask[4:8, 4:8] = 255
    _normal, material = mm.generate(image, {'emission_threshold': 0.1},
                                    emission_mask=mask)
    assert np.array_equal(255 - material[:, :, 3], mask)


def test_transparent_texels_are_flat_and_dark():
    """What hides under alpha 0 must not show up in a decal's maps."""
    rng_free = (np.arange(48 * 48 * 3) * 37 % 251).astype(np.uint8).reshape(48, 48, 3)
    image = np.dstack([rng_free, np.zeros((48, 48), np.uint8)])
    normal, material = mm.generate(image, {'emission_threshold': 0.1})
    assert (normal[:, :, :3] == (128, 128, 255)).all()
    assert (material[:, :, 3] == 255).all()


def test_generation_is_fast_enough_for_a_load_time_fallback():
    y, x = np.mgrid[0:1024, 0:1024]
    image = np.dstack([(x * 3 + y) % 256, (x + y * 5) % 256, (x ^ y) % 256]).astype(np.uint8)
    import time
    started = time.perf_counter()
    mm.generate(image)
    assert time.perf_counter() - started < 5.0       # ~0.15 s; generous for CI


# -- which files get maps ------------------------------------------------------

@pytest.mark.parametrize('rel', ['nodraw.jpg', 'NoDraw_blue.png', 'trigger.jpg',
                                 'default.png', 'black.jpeg', 'Dev/128.jpg',
                                 'dev/grid.png'])
def test_tool_textures(rel):
    assert mm.is_tool_texture(rel)
    assert mm.flat_values(rel) == mm.FLAT_VALUES


@pytest.mark.parametrize('rel', ['cobblestone.jpg', 'terrain/rock.jpg',
                                 'decals/vent.jpg', 'developer.png'])
def test_ordinary_textures_are_surfaces(rel):
    assert not mm.is_tool_texture(rel)
    assert mm.flat_values(rel) is None
    assert mm.is_surface_texture(rel)


@pytest.mark.parametrize('rel', ['water_normal.png', 'cookies/Spot.jpg',
                                 'effects/explosion.png', 'effects/fire01.gif',
                                 'effects/test', 'cobblestone_n.webp',
                                 'cobblestone_m.png', 'terrain/rock_h.png',
                                 'cobblestone.mat.json', 'readme.txt'])
def test_things_that_never_get_maps(rel):
    assert not mm.is_surface_texture(rel)


# -- sidecars and status -------------------------------------------------------

def _write_image(path, value=120):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.full((8, 8, 3), value, np.uint8)).save(str(path))
    return str(path)


def test_companion_paths_prefers_png_then_webp_then_jpg(tmp_path):
    diffuse = _write_image(tmp_path / 'wall.jpg')
    assert mm.companion_paths(diffuse) == {
        'normal': None, 'material': None, 'height': None, 'sidecar': None}
    _write_image(tmp_path / 'wall_n.jpg')
    _write_image(tmp_path / 'wall_n.webp')
    _write_image(tmp_path / 'wall_m.jpg')
    _write_image(tmp_path / 'wall_h.png')
    (tmp_path / 'wall.mat.json').write_text('{}')
    found = mm.companion_paths(diffuse)
    assert found['normal'] == str(tmp_path / 'wall_n.webp')
    assert found['material'] == str(tmp_path / 'wall_m.jpg')
    assert found['height'] == str(tmp_path / 'wall_h.png')
    assert found['sidecar'] == str(tmp_path / 'wall.mat.json')
    _write_image(tmp_path / 'wall_n.png')
    assert mm.companion_paths(diffuse)['normal'] == str(tmp_path / 'wall_n.png')


def test_load_params_tolerates_missing_and_bad_files(tmp_path):
    assert mm.load_params(str(tmp_path / 'absent.mat.json')) == mm.DEFAULT_PARAMS
    bad = tmp_path / 'bad.mat.json'
    bad.write_text('{ not json')
    assert mm.load_params(str(bad)) == mm.DEFAULT_PARAMS
    listy = tmp_path / 'list.mat.json'
    listy.write_text('[1, 2]')
    assert mm.load_params(str(listy)) == mm.DEFAULT_PARAMS


def test_sidecar_round_trip(tmp_path):
    path = str(tmp_path / 'wall.mat.json')
    params = dict(mm.DEFAULT_PARAMS, metal=0.9, parallax=2.5, emission=3.0)
    mm.save_params(path, params, {'version': mm.GENERATOR_VERSION,
                                  'source_sha256': 'abc'})
    raw = json.loads(open(path).read())
    assert raw['parallax'] == 2.5 and raw['emission'] == 3.0      # the renderer's keys
    assert raw['generated'] == {'version': mm.GENERATOR_VERSION, 'source_sha256': 'abc'}
    assert mm.load_params(path) == params                         # no "generated" in it
    mm.save_params(path, {'metal': 0.2}, None)
    assert 'generated' not in json.loads(open(path).read())
    assert mm.load_params(path)['metal'] == 0.2
    assert mm.load_params(path)['parallax'] == mm.DEFAULT_PARAMS['parallax']


def test_status_names_each_kind(tmp_path):
    textures = tmp_path / 'textures'

    tool = _write_image(textures / 'nodraw.jpg')
    dev = _write_image(textures / 'Dev' / '64.jpg')
    assert mm.status(tool) == 'tool'
    assert mm.status(dev) == 'tool'

    bare = _write_image(textures / 'bare.png')
    assert mm.status(bare) == 'runtime'

    painted = _write_image(textures / 'painted.png')
    _write_image(textures / 'painted_n.png')
    _write_image(textures / 'painted_m.png')
    assert mm.status(painted) == 'hand-made'
    mm.save_params(mm.sidecar_path(painted), {'parallax': 3.0})    # tuned, still painted
    assert mm.status(painted) == 'hand-made'

    baked = _write_image(textures / 'baked.png')
    _write_image(textures / 'baked_n.webp')
    _write_image(textures / 'baked_m.webp')
    with open(baked, 'rb') as handle:
        digest = mm.sha256_bytes(handle.read())
    mm.save_params(mm.sidecar_path(baked), {}, {'version': mm.GENERATOR_VERSION,
                                                'source_sha256': digest})
    assert mm.status(baked) == 'generated'
    assert mm.is_current(baked)
    _write_image(textures / 'baked.png', value=30)                 # the artwork changed
    assert mm.status(baked) == 'generated'
    assert not mm.is_current(baked)

    assert mm.status(_write_image(textures / 'cookies' / 'spot.png')) is None


# -- the runtime cache ---------------------------------------------------------

def test_cache_key_depends_on_content_params_and_version(monkeypatch):
    data = _png_bytes(_pattern())
    key = mm.cache_key(data)
    assert key == mm.cache_key(data, dict(mm.DEFAULT_PARAMS))
    assert len(key) == 64 and int(key, 16) >= 0
    assert mm.cache_key(data + b'\0') != key
    assert mm.cache_key(data, {'metal': 1.0}) != key
    # The renderer's own knobs do not change the maps, so not the key either.
    assert mm.cache_key(data, {'parallax': 9.0, 'emission': 4.0}) == key
    monkeypatch.setattr(mm, 'GENERATOR_VERSION', mm.GENERATOR_VERSION + 1)
    assert mm.cache_key(data) != key


def test_cache_dir_honours_the_override(monkeypatch, tmp_path):
    monkeypatch.setenv('FIO_MATERIAL_CACHE', str(tmp_path / 'maps'))
    assert mm.cache_dir() == str(tmp_path / 'maps')
    monkeypatch.delenv('FIO_MATERIAL_CACHE')
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'xdg'))
    assert mm.cache_dir().endswith(os.path.join('Fio', 'cache', 'materials'))
    assert ROOT not in mm.cache_dir()


def test_ensure_cached_writes_once_and_reuses(tmp_path, monkeypatch):
    image = _pattern()
    data = _png_bytes(image)
    cache = str(tmp_path / 'cache')
    paths = mm.ensure_cached(data, cache=cache)
    assert paths is not None
    normal_path, material_path = paths
    key = mm.cache_key(data)
    assert os.path.basename(normal_path) == key + '_n.png'
    assert os.path.basename(material_path) == key + '_m.png'
    assert sorted(os.listdir(cache)) == sorted([key + '_m.png', key + '_n.png'])

    normal, material = mm.generate(image)
    assert np.array_equal(np.asarray(Image.open(normal_path)), normal)
    assert np.array_equal(np.asarray(Image.open(material_path)), material)

    # A second call must not regenerate or rewrite.
    for path in paths:
        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
    monkeypatch.setattr(mm, 'generate', lambda *a, **k: pytest.fail('regenerated'))
    assert mm.ensure_cached(data, cache=cache) == paths
    assert all(os.stat(path).st_mtime_ns == 1_000_000_000 for path in paths)
    monkeypatch.undo()

    other = mm.ensure_cached(data, {'metal': 1.0}, cache=cache)
    assert other is not None and other != paths
    assert len(os.listdir(cache)) == 4

    monkeypatch.setattr(mm, 'GENERATOR_VERSION', mm.GENERATOR_VERSION + 1)
    newer = mm.ensure_cached(data, cache=cache)
    assert newer is not None and newer != paths
    assert len(os.listdir(cache)) == 6


def test_ensure_cached_uses_the_default_cache_dir(tmp_path, monkeypatch):
    monkeypatch.setenv('FIO_MATERIAL_CACHE', str(tmp_path / 'env-cache'))
    normal_path, _material_path = mm.ensure_cached(_png_bytes(_pattern(16, 16)))
    assert os.path.dirname(normal_path) == str(tmp_path / 'env-cache')


def test_ensure_cached_returns_none_for_an_unreadable_image(tmp_path):
    cache = str(tmp_path / 'cache')
    assert mm.ensure_cached(b'this is not an image', cache=cache) is None
    assert mm.ensure_cached(b'', cache=cache) is None
    assert not os.path.isdir(cache) or os.listdir(cache) == []


# -- the stock assets ----------------------------------------------------------

def test_every_stock_surface_texture_has_maps():
    missing = []
    surfaces = 0
    for rel, path in _stock():
        if not mm.is_surface_texture(rel) or mm.is_tool_texture(rel):
            continue
        surfaces += 1
        found = mm.companion_paths(path)
        if not (found['normal'] and found['material']):
            missing.append(rel)
    assert surfaces > 40
    assert missing == [], 'run tools/make_material_maps.py'


def test_stock_generated_maps_are_current():
    """A changed image or a changed sidecar must be re-baked before commit."""
    stale = [rel for rel, path in _stock()
             if mm.status(path) == 'generated' and not mm.is_current(path)]
    assert stale == [], 'run tools/make_material_maps.py'


def test_stock_maps_have_the_diffuse_resolution():
    wrong = []
    for rel, path in _stock():
        found = mm.companion_paths(path)
        if mm.is_companion(rel) or not (found['normal'] or found['material']):
            continue
        with Image.open(path) as image:
            size = image.size
        for key in ('normal', 'material'):
            if found[key]:
                with Image.open(found[key]) as companion:
                    # RGB is a map whose alpha is 255 everywhere (no emission).
                    if companion.size != size or companion.mode not in ('RGB', 'RGBA'):
                        wrong.append((rel, key, companion.size, companion.mode))
    assert wrong == []


def test_tool_and_non_surface_textures_have_no_companions():
    offenders = []
    checked = set()
    for rel, path in _stock():
        if mm.is_companion(rel):
            continue
        if mm.is_tool_texture(rel) or not mm.is_surface_texture(rel):
            checked.add(rel)
            if any(mm.companion_paths(path).values()):
                offenders.append(rel)
    assert offenders == []
    assert 'water_normal.png' in checked
    assert any(rel.startswith('cookies/') for rel in checked)
    assert any(rel.startswith('effects/') for rel in checked)
    assert 'nodraw.jpg' in checked


def test_no_orphaned_companions_in_the_stock_textures():
    """Every map and sidecar belongs to a diffuse image that is still there."""
    names = {rel for rel, _path in _stock()}
    stems = {os.path.splitext(rel)[0] for rel in names if not mm.is_companion(rel)}
    orphans = []
    for rel in names:
        if not mm.is_companion(rel):
            continue
        if rel.endswith(mm.SIDECAR_SUFFIX):
            owner = rel[:-len(mm.SIDECAR_SUFFIX)]
        else:
            owner = os.path.splitext(rel)[0][:-2]
        if owner not in stems:
            orphans.append(rel)
    assert orphans == []


def test_the_tool_agrees_the_stock_assets_are_complete(capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'make_material_maps', os.path.join(ROOT, 'tools', 'make_material_maps.py'))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert tool.main(['--root', os.path.join(ROOT, 'assets'), '--check']) == 0
    capsys.readouterr()


def test_the_tool_bakes_checks_and_respects_hand_made_maps(tmp_path, capsys):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'make_material_maps', os.path.join(ROOT, 'tools', 'make_material_maps.py'))
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)

    root = tmp_path / 'assets'
    textures = root / 'textures'
    textures.mkdir(parents=True)
    Image.fromarray(_pattern(16, 16)).save(str(textures / 'metal_wall.png'))
    _write_image(textures / 'nodraw.jpg')
    _write_image(textures / 'cookies' / 'spot.png')
    painted = _write_image(textures / 'painted.png')
    _write_image(textures / 'painted_n.png', value=7)
    _write_image(textures / 'painted_m.png', value=9)
    args = ['--root', str(root)]

    assert tool.main(args + ['--check']) == 1          # metal_wall has nothing yet
    assert tool.main(args) == 0
    assert tool.main(args + ['--check']) == 0

    wall = str(textures / 'metal_wall.png')
    assert mm.status(wall) == 'generated' and mm.is_current(wall)
    assert mm.load_params(mm.sidecar_path(wall))['metal'] > 0.5      # the name table
    normal, material = mm.generate(np.asarray(Image.open(wall).convert('RGBA')),
                                   mm.load_params(mm.sidecar_path(wall)))
    # (WebP drops an alpha channel that is 255 throughout: the file then opens
    # as RGB, and converting to RGBA puts the 255 back.)
    for name, expected in (('metal_wall_n.webp', normal), ('metal_wall_m.webp', material)):
        with Image.open(str(textures / name)) as image:
            assert np.array_equal(np.asarray(image.convert('RGBA')), expected)

    # Hand-made maps and tool textures are left exactly as they were.
    assert mm.status(painted) == 'hand-made'
    assert np.asarray(Image.open(str(textures / 'painted_n.png')))[0, 0, 0] == 7
    assert not os.path.exists(mm.sidecar_path(painted))
    assert sorted(os.listdir(str(textures / 'cookies'))) == ['spot.png']
    assert not any(name.startswith('nodraw_') or name.startswith('nodraw.mat')
                   for name in os.listdir(str(textures)))

    # Tuning the sidecar makes the maps stale; a run rebuilds with the tuning.
    before = os.stat(str(textures / 'metal_wall_n.webp')).st_mtime_ns
    assert tool.main(args) == 0                        # current: untouched
    assert os.stat(str(textures / 'metal_wall_n.webp')).st_mtime_ns == before
    sidecar = json.loads(open(mm.sidecar_path(wall)).read())
    sidecar['metal'] = 0.25
    open(mm.sidecar_path(wall), 'w').write(json.dumps(sidecar))
    assert tool.main(args + ['--check']) == 1
    assert tool.main(args) == 0
    assert tool.main(args + ['--check']) == 0
    assert mm.load_params(mm.sidecar_path(wall))['metal'] == 0.25
    assert np.asarray(Image.open(str(textures / 'metal_wall_m.webp')))[0, 0, 2] == 64
    capsys.readouterr()
