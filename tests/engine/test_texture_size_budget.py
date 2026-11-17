"""A texture's decoded size is budgeted before anything is decoded.

A GIF's frames are composited: each one is a full-canvas RGBA image however
few bytes the file holds. A 959-byte GIF declaring a 4096x4096 canvas and 40
one-pixel frames used to cost 2.6 GiB to load as a brush texture.
"""

import glob
import io
import os
import struct
import zipfile

import pytest

pytest.importorskip("PIL", reason="textures are decoded with Pillow")

from PIL import Image                                         # noqa: E402

from engine import animated_texture                           # noqa: E402
from engine.animated_texture import (                         # noqa: E402
    MAX_GIF_DIMENSION, MAX_GIF_FRAMES, MAX_GIF_TOTAL_PIXELS, MAX_TEXTURE_PIXELS,
    TextureTooLarge, decode_gif_frames, frame_budget, frames_allowed,
    is_animated_gif, open_still_rgba,
)
from tests.engine.test_animated_texture import COLOURS, write_gif  # noqa: E402

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, os.pardir))


def bomb_gif(path, side=4096, frames=40):
    """A tiny GIF: *frames* one-pixel frames on a *side* x *side* canvas."""
    out = io.BytesIO()
    out.write(b"GIF89a" + struct.pack("<HHBBB", side, side, 0x80, 0, 0)
              + b"\x00\x00\x00\xff\xff\xff")
    out.write(b"\x21\xff\x0bNETSCAPE2.0\x03\x01\x00\x00\x00")
    for index in range(frames):
        out.write(b"\x21\xf9\x04\x04\x01\x00\x00\x00")
        out.write(b"\x2c" + struct.pack("<HHHHB", index % 100, 0, 1, 1, 0)
                  + b"\x02\x02\x44\x01\x00")
    out.write(b"\x3b")
    path.write_bytes(out.getvalue())
    return path


def test_a_decompression_bomb_gif_decodes_one_frame_and_says_so(tmp_path, capsys):
    path = bomb_gif(tmp_path / "bomb.gif")
    assert path.stat().st_size < 2000 and is_animated_gif(path)
    images, seconds = decode_gif_frames(path)
    decoded = sum(im.width * im.height for im in images)
    assert len(images) == 1 and len(seconds) == 1, (
        f"{len(images)} frames ({decoded} pixels) decoded from a {path.stat().st_size}-byte GIF")
    assert decoded <= MAX_GIF_TOTAL_PIXELS
    out = capsys.readouterr().out
    assert "too large to animate" in out and "first frame only" in out, out
    decode_gif_frames(path)
    assert "too large" not in capsys.readouterr().out, "the warning is given once per file"


def test_too_many_frames_or_too_wide_a_canvas_is_not_animated():
    assert frames_allowed(64, 64, MAX_GIF_FRAMES) == MAX_GIF_FRAMES
    assert frames_allowed(64, 64, MAX_GIF_FRAMES + 1, "many") == 1
    assert frames_allowed(MAX_GIF_DIMENSION + 1, 8, 2, "wide") == 1
    assert frames_allowed(MAX_GIF_DIMENSION, 8, 2) == 2
    assert frames_allowed(2000, 2000, 17, "heavy") == 1          # 68 Mpx
    assert frames_allowed(2000, 2000, 16) == 16                  # 64 Mpx


def test_a_picture_too_large_to_be_a_texture_is_refused_not_decoded(tmp_path):
    with pytest.raises(TextureTooLarge):
        frames_allowed(8193, 8192, 1, "huge")
    with pytest.raises(TextureTooLarge):
        frames_allowed(0, 10, 1, "empty")
    path = bomb_gif(tmp_path / "huge.gif", side=9000, frames=2)
    with pytest.raises(TextureTooLarge):
        decode_gif_frames(path)


def test_a_still_image_over_fios_limit_is_refused_from_its_header(tmp_path, monkeypatch):
    Image.new("RGB", (64, 32), (9, 8, 7)).save(tmp_path / "ok.png")
    image = open_still_rgba(tmp_path / "ok.png")
    assert image.mode == "RGBA" and image.size == (64, 32) and image.getpixel((1, 1)) == (9, 8, 7, 255)
    # The limit is Fio's own, not Pillow's default: shrink it rather than
    # write a 67-megapixel file.
    assert MAX_TEXTURE_PIXELS < Image.MAX_IMAGE_PIXELS
    monkeypatch.setattr(animated_texture, "MAX_TEXTURE_PIXELS", 64 * 32 - 1)
    with pytest.raises(TextureTooLarge, match="ok.png"):
        open_still_rgba(tmp_path / "ok.png")


def test_an_ordinary_animated_gif_is_unaffected(tmp_path):
    images, seconds = decode_gif_frames(write_gif(tmp_path / "anim.gif"))
    assert [im.getpixel((3, 3))[:3] for im in images] == list(COLOURS)
    assert len(seconds) == 3


def _shipped_gifs():
    for path in sorted(glob.glob(os.path.join(ROOT, "assets", "**", "*.gif"), recursive=True)
                       + glob.glob(os.path.join(ROOT, "plugins", "**", "*.gif"), recursive=True)):
        yield os.path.relpath(path, ROOT), path
    for archive in sorted(glob.glob(os.path.join(ROOT, "assets", "*.fiopak"))):
        with zipfile.ZipFile(archive) as handle:
            for name in handle.namelist():
                if name.lower().endswith(".gif"):
                    yield f"{os.path.basename(archive)}:{name}", io.BytesIO(handle.read(name))


def test_every_shipped_gif_animates_with_a_wide_margin():
    checked = 0
    for label, source in _shipped_gifs():
        with Image.open(source) as gif:
            frames = getattr(gif, "n_frames", 1)
            total = frames * gif.width * gif.height
            assert frame_budget(gif, label) == frames, f"{label} lost its animation"
            assert total * 3 <= MAX_GIF_TOTAL_PIXELS, (
                f"{label}: {total} px is within 3x of the {MAX_GIF_TOTAL_PIXELS} px budget")
            assert frames * 4 <= MAX_GIF_FRAMES and max(gif.size) * 4 <= MAX_GIF_DIMENSION, label
        checked += 1
    if not checked:
        pytest.skip("no GIF assets in this checkout")
