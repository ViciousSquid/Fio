"""Decoding animated GIF textures and choosing the frame for a play clock."""

import pytest

pytest.importorskip("PIL", reason="GIF textures are decoded with Pillow")

from PIL import Image                                         # noqa: E402

from engine.animated_texture import (                         # noqa: E402
    cumulative, decode_gif_frames, frame_at, is_animated_gif,
)

COLOURS = ((255, 0, 0), (0, 255, 0), (0, 0, 255))


def write_gif(path, durations=(50, 100, 70), size=(16, 8)):
    frames = [Image.new("RGB", size, c) for c in COLOURS[:len(durations)]]
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=list(durations), loop=0)
    return path


def test_only_a_gif_with_several_frames_is_animated(tmp_path):
    assert is_animated_gif(write_gif(tmp_path / "anim.gif"))
    assert not is_animated_gif(write_gif(tmp_path / "still.gif", durations=(100,)))
    Image.new("RGB", (4, 4)).save(tmp_path / "x.png")
    assert not is_animated_gif(tmp_path / "x.png")
    (tmp_path / "broken.gif").write_bytes(b"GIF89a nonsense")
    assert not is_animated_gif(tmp_path / "broken.gif")


def test_frames_decode_whole_with_their_times(tmp_path):
    images, seconds = decode_gif_frames(write_gif(tmp_path / "anim.gif"))
    assert [im.getpixel((3, 3))[:3] for im in images] == list(COLOURS)
    assert all(im.size == (16, 8) and im.mode == "RGBA" for im in images)
    assert seconds == pytest.approx([0.05, 0.1, 0.07])


def test_a_zero_delay_frame_shows_for_a_tenth_of_a_second(tmp_path):
    _images, seconds = decode_gif_frames(write_gif(tmp_path / "fast.gif", durations=(0, 20, 30)))
    assert seconds == pytest.approx([0.1, 0.02, 0.03])


@pytest.mark.parametrize("clock, frame", [
    (0.0, 0), (0.049, 0), (0.051, 1), (0.149, 1), (0.151, 2), (0.219, 2),
    (0.221, 0), (0.272, 1), (-1.0, 0),
])
def test_the_frame_for_a_clock_loops(clock, frame):
    assert frame_at(cumulative([0.05, 0.1, 0.07]), clock) == frame
