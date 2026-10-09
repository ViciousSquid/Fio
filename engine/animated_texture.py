"""Animated GIF textures: decoding, and which frame shows when.

A GIF on a brush face is an ordinary texture whose GL name never changes: the
renderer uploads its first frame like any image, so Fit, Natural, scale and
stretch in the Surface Inspector read the same pixel size as for a PNG, and
in Play it re-uploads the frame due at the play clock into the same texture
(:meth:`ResourcesMixin.animate_textures`). Outside Play the first frame shows.

GL- and Qt-free; Pillow does the decoding (it composites each GIF frame with
the previous one, so every frame is the whole picture).
"""

import bisect

#: Shortest frame time honoured: browsers treat 0-10 ms GIF delays as 100 ms
#: for the same reason (a 0 delay would spin).
MIN_FRAME_SECONDS = 0.02
DEFAULT_FRAME_SECONDS = 0.1


def is_animated_gif(path) -> bool:
    """True for a GIF with more than one frame."""
    if not str(path).lower().endswith(".gif"):
        return False
    try:
        from PIL import Image
        with Image.open(path) as image:
            return bool(getattr(image, "is_animated", False)) and image.n_frames > 1
    except Exception:
        return False


def _frame_seconds(frame) -> float:
    try:
        ms = float(frame.info.get("duration", DEFAULT_FRAME_SECONDS * 1000.0))
    except (TypeError, ValueError):
        return DEFAULT_FRAME_SECONDS
    seconds = ms / 1000.0
    return DEFAULT_FRAME_SECONDS if seconds < 0.011 else max(MIN_FRAME_SECONDS, seconds)


def decode_gif_frames(path):
    """``([RGBA PIL images], [seconds per frame])`` for every frame of *path*.

    Frames that come out at another size than the first (a malformed file)
    are fitted to it, so every frame can be uploaded into one texture.
    """
    from PIL import Image, ImageSequence
    images, seconds = [], []
    with Image.open(path) as gif:
        for frame in ImageSequence.Iterator(gif):
            rgba = frame.convert("RGBA")
            if images and rgba.size != images[0].size:
                rgba = rgba.resize(images[0].size)
            images.append(rgba)
            seconds.append(_frame_seconds(frame))
    return images, seconds


def cumulative(seconds):
    """End time of each frame: ``[0.1, 0.2, ...]`` for 0.1 s frames."""
    out, total = [], 0.0
    for s in seconds:
        total += s
        out.append(total)
    return out


def frame_at(ends, clock) -> int:
    """The frame showing *clock* seconds into a loop whose frames end at *ends*."""
    if not ends or clock <= 0.0:
        return 0
    t = clock % ends[-1]
    return min(bisect.bisect_right(ends, t), len(ends) - 1)
