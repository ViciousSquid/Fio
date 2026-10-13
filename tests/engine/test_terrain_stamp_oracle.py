"""An independent oracle for placed terrain stamps.

The engine keeps stamp paint up to date incrementally (a chunk index, deferred
drags, new stamps baked on top, a tinted display layer) and caches what
``to_dict`` serialises. This oracle shares none of that: after every step of
a random edit sequence it rebuilds the paint the way stamps were baked before
they were movable -- each stamp painted in order onto a fresh terrain with
``paint_texture_at`` over the base paint -- and serialises everything from
scratch, then checks the engine against it:

* the stored paint equals the rebuilt paint, texel for texel;
* ``to_dict`` equals an uncached serialisation of the same state;
* what would be uploaded to the GPU is the stored paint, except inside the
  footprints of selected stamps, and nothing else carries a paint version;
* a save/load round trip reproduces the stamps and the paint.
"""

import base64
import io
import json
import random
import zlib

import numpy as np
import pytest

pytest.importorskip("PyQt5", reason="stamp textures are loaded with QImage")

from engine.terrain import Terrain                            # noqa: E402


@pytest.fixture(scope="module")
def textures(tmp_path_factory):
    """An opaque texture and a semi-transparent one with an alpha ramp."""
    from PyQt5.QtGui import QColor, QImage
    root = tmp_path_factory.mktemp("stamp_oracle")
    solid = QImage(8, 8, QImage.Format_RGBA8888)
    solid.fill(QColor(30, 160, 90, 255))
    ramp = QImage(16, 16, QImage.Format_RGBA8888)
    for y in range(16):
        for x in range(16):
            ramp.setPixelColor(x, y, QColor(220, 40 + 10 * x, 10 * y, 40 + 13 * x))
    paths = [str(root / "solid.png"), str(root / "ramp.png")]
    assert solid.save(paths[0]) and ramp.save(paths[1])
    return paths


# -- The oracle ------------------------------------------------------------------

def _reference_paint(terrain):
    """The paint rebuilt from scratch: base paint, then every stamp in order."""
    ref = Terrain(seed=0)
    ref.chunk_size, ref.offset_x, ref.offset_z = (
        terrain.chunk_size, terrain.offset_x, terrain.offset_z)
    ref.texture_paint_maps = {k: v.copy() for k, v in terrain.texture_paint_base.items()}
    for s in terrain.paint_stamps:
        ref.paint_texture_at(s["x"], s["z"], s["radius"], s["texture"],
                             feather=s["feather"], opacity=s["opacity"],
                             angle=s["angle"])
    return {k: v for k, v in ref.texture_paint_maps.items() if np.any(v[..., 3])}


def _encode(maps):
    out = []
    for (cx, cz), paint in sorted(maps.items()):
        if np.any(paint[..., 3]):
            raw = zlib.compress(np.ascontiguousarray(paint).tobytes(), 6)
            out.append({"coord": [cx, cz], "data": base64.b64encode(raw).decode("ascii")})
    return out


def _reference_dict_parts(terrain):
    parts = {}
    paint = _encode(terrain.texture_paint_maps)
    if paint:
        parts["texture_paint"] = paint
    if terrain.paint_stamps:
        parts["paint_stamps"] = [dict(s) for s in terrain.paint_stamps]
        base = _encode(terrain.texture_paint_base)
        if base:
            parts["texture_paint_base"] = base
    if terrain.sculpt_offsets:
        parts["sculpt_offsets"] = [[gx, gz, v] for (gx, gz), v in terrain.sculpt_offsets.items()]
    if terrain.heightmap_data is not None:
        buf = io.BytesIO()
        np.save(buf, terrain.heightmap_data)
        parts["heightmap_blob"] = base64.b64encode(buf.getvalue()).decode("ascii")
    return parts


def _footprint_mask(terrain, key, stamps):
    """Texels of chunk *key* inside any of *stamps* (bounding squares)."""
    res = terrain.TERRAIN_PAINT_RESOLUTION
    cell = terrain.chunk_size / res
    xs = terrain.offset_x + key[0] * terrain.chunk_size + (np.arange(res) + 0.5) * cell
    zs = terrain.offset_z + key[1] * terrain.chunk_size + (np.arange(res) + 0.5) * cell
    wx, wz = np.meshgrid(xs, zs, indexing="ij")
    mask = np.zeros((res, res), bool)
    for s in stamps:
        r = s["radius"] + cell
        mask |= (np.abs(wx - s["x"]) <= r) & (np.abs(wz - s["z"]) <= r)
    return mask


def _check(terrain, step):
    """Compare the engine with the oracle; *step* names the failing edit."""
    terrain.finish_stamp_edit()     # stored paint is only due after a drag ends
    ref = _reference_paint(terrain)
    stored = {k: v for k, v in terrain.texture_paint_maps.items() if np.any(v[..., 3])}
    assert stored.keys() == ref.keys(), step
    for key in ref:
        assert np.array_equal(stored[key], ref[key]), (step, key)

    data = terrain.to_dict()
    for name, expected in _reference_dict_parts(terrain).items():
        assert data.get(name) == expected, (step, name)
    for name in ("texture_paint", "paint_stamps", "texture_paint_base",
                 "sculpt_offsets", "heightmap_blob"):
        if name not in _reference_dict_parts(terrain):
            assert name not in data, (step, name)

    selected = [s for s in terrain.paint_stamps if s["id"] in terrain.selected_stamp_ids]
    if not selected:
        assert not terrain._paint_display, step
    for key, shown in terrain._paint_display.items():
        base = stored.get(key, np.zeros_like(shown))
        outside = ~_footprint_mask(terrain, key, selected)
        assert np.array_equal(shown[outside], base[outside]), (step, key)
    painted = stored.keys() | terrain._paint_display.keys()
    assert set(terrain._paint_map_versions) <= painted | set(terrain.texture_paint_maps), step
    for key in painted:
        assert terrain._paint_map_versions.get(key, 0) > 0, (step, key)

    reloaded = Terrain(seed=0)
    reloaded.from_dict(json.loads(json.dumps(data)))
    assert reloaded.paint_stamps == terrain.paint_stamps, step
    again = {k: v for k, v in reloaded.texture_paint_maps.items() if np.any(v[..., 3])}
    assert again.keys() == ref.keys(), step
    for key in ref:
        assert np.array_equal(again[key], ref[key]), (step, key)


# -- The random edit sequence -------------------------------------------------------

def _random_stamp_args(rng, textures):
    return dict(world_x=rng.uniform(-700, 700), world_z=rng.uniform(-700, 700),
                radius=rng.choice([3.0, rng.uniform(4, 60), rng.uniform(60, 300)]),
                texture=rng.choice(textures), feather=rng.choice([0.0, 0.25, 0.8]),
                opacity=rng.choice([1.0, 0.5]), angle=rng.choice([0.0, 0.7]))


@pytest.mark.slow
@pytest.mark.parametrize("seed", range(6))
def test_engine_matches_the_oracle_through_random_edits(seed, textures, monkeypatch):
    rng = random.Random(seed)
    if seed % 2:
        # Odd seeds take the vector lookup path with only a few stamps.
        monkeypatch.setattr(Terrain, "STAMP_VECTOR_MIN", 3)
    terrain = Terrain(seed=seed)
    if seed % 3 == 0:
        # A map painted before stamps were records: its paint is the base.
        old = Terrain(seed=seed)
        old.paint_texture_at(40.0, -30.0, 90.0, textures[1], feather=0.3)
        terrain.from_dict(old.to_dict())

    for step in range(40):
        ids = [s["id"] for s in terrain.paint_stamps]
        op = rng.choice(["add", "add", "add", "move", "drag", "select", "merge",
                         "free_paint", "sculpt", "heightmap", "reload", "to_dict"]
                        + (["clear"] if step % 17 == 16 else []))
        if op == "add":
            if rng.random() < 0.5:
                terrain.set_selected_stamps(())      # as the viewport does
            else:
                # Keep a selection (and so a tinted display) and add near it.
                terrain.set_selected_stamps(set(ids[-2:]))
            args = _random_stamp_args(rng, textures)
            if ids and rng.random() < 0.5:
                near = terrain.paint_stamps[-1]
                args["world_x"], args["world_z"] = near["x"] + 20.0, near["z"] - 20.0
            terrain.add_paint_stamp(**args)
        elif op == "move" and ids:
            terrain.move_paint_stamps(set(rng.sample(ids, min(len(ids), rng.randint(1, 3)))),
                                      rng.uniform(-300, 300), rng.uniform(-300, 300))
        elif op == "drag" and ids:
            chosen = set(rng.sample(ids, min(len(ids), rng.randint(1, 2))))
            terrain.set_selected_stamps(chosen)
            for _ in range(rng.randint(1, 5)):
                terrain.move_paint_stamps(chosen, rng.uniform(-90, 90),
                                          rng.uniform(-90, 90), defer=True)
            terrain.finish_stamp_edit()
        elif op == "select":
            terrain.set_selected_stamps(set(rng.sample(ids, rng.randint(0, len(ids)))))
        elif op == "merge":
            records = [dict(s) for s in rng.sample(terrain.paint_stamps,
                                                    rng.randint(0, len(ids)))]
            for record in records[: len(records) // 2]:
                record["x"] += rng.uniform(-50, 50)
            new = Terrain.make_paint_stamp(**_random_stamp_args(rng, textures))
            terrain.merge_paint_stamps(records + [new, dict(new)])
        elif op == "free_paint":
            args = _random_stamp_args(rng, textures)
            terrain.paint_texture_at(args["world_x"], args["world_z"], args["radius"],
                                     args["texture"], feather=args["feather"])
        elif op == "sculpt":
            terrain.apply_sculpt_at(rng.uniform(-500, 500), rng.uniform(-500, 500),
                                    rng.uniform(8, 64), rng.uniform(-5, 5))
        elif op == "heightmap":
            terrain.heightmap_data = (None if rng.random() < 0.3 else
                                      np.random.default_rng(step).random((8, 8)).astype(np.float32))
        elif op == "reload":
            terrain.from_dict(json.loads(json.dumps(terrain.to_dict())))
        elif op == "to_dict":
            terrain.to_dict()
        elif op == "clear":
            terrain.clear_texture_paint()
        _check(terrain, f"seed {seed} step {step}: {op}")
