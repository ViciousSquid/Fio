"""Animated GIF textures: decoding, and which frame shows when.

A GIF on a brush face is an ordinary texture whose GL name never changes: the
renderer uploads its first frame like any image, so Fit, Natural, scale and
stretch in the Surface Inspector read the same pixel size as for a PNG, and
in Play it re-uploads the frame due at the play clock into the same texture
(:meth:`ResourcesMixin.animate_textures`). Outside Play the first frame shows.

GL- and Qt-free; Pillow does the decoding (it composites each GIF frame with
the previous one, so every frame is the whole picture).

Every frame is therefore a full-canvas RGBA image, whatever the file's size:
a 1 KB GIF can declare a 4096x4096 canvas and dozens of frames. The decoded
size is budgeted before anything is decoded (:func:`frame_budget`); a GIF
over budget shows its first frame only, and a picture too large to be a
texture at all is refused (:func:`open_still_rgba` for still images).
"""

import bisect

#: Largest picture loaded as a texture, in pixels and along one side
#: (8192 x 8192 RGBA is 256 MiB). Fio's largest shipped image is 1.6 Mpx.
MAX_TEXTURE_PIXELS = 8192 * 8192
MAX_TEXTURE_DIMENSION = 16384
#: An animation's budget: frames x width x height (64 Mpx of RGBA is
#: 256 MiB; the largest shipped GIF, fire01, is 19.3 Mpx and 98 frames),
#: its frame count, and one side of its canvas.
MAX_GIF_TOTAL_PIXELS = 64 * 1000 * 1000
MAX_GIF_FRAMES = 512
MAX_GIF_DIMENSION = 4096

_warned = set()


def _warn_once(label, text):
    key = str(label)
    if key in _warned:
        return
    if len(_warned) < 1024:
        _warned.add(key)
    print(f"[Texture] {text}")


class TextureTooLarge(ValueError):
    """A picture whose decoded size is over Fio's texture limit."""


def check_texture_size(width, height, label=""):
    """Raise :class:`TextureTooLarge` for a picture too large to load."""
    width, height = int(width), int(height)
    if (width <= 0 or height <= 0
            or max(width, height) > MAX_TEXTURE_DIMENSION
            or width * height > MAX_TEXTURE_PIXELS):
        raise TextureTooLarge(
            f"image {str(label)[:120]!r} is {width}x{height}; the limit is "
            f"{MAX_TEXTURE_PIXELS} pixels and {MAX_TEXTURE_DIMENSION} a side")


def open_still_rgba(path):
    """The still image at *path* (or archived there) as an RGBA PIL image.

    The size in the file's header is checked before any pixel is decoded,
    with Fio's own limit rather than Pillow's much larger default.
    """
    from PIL import Image
    with Image.open(_source(path)) as image:
        check_texture_size(image.width, image.height, path)
        return image.convert("RGBA")


def frames_allowed(width, height, frames, label="") -> int:
    """How many of *frames* full-canvas frames of an animation may be decoded.

    All of them when the animation is within budget, 1 (the first frame, a
    still picture) when it has too many frames, too large a canvas for an
    animation or too many pixels in total; raises :class:`TextureTooLarge`
    when even one frame is too large to be a texture.
    """
    check_texture_size(width, height, label)
    frames = int(frames or 1)
    if frames <= 1:
        return 1
    if (frames > MAX_GIF_FRAMES or max(width, height) > MAX_GIF_DIMENSION
            or frames * width * height > MAX_GIF_TOTAL_PIXELS):
        _warn_once(label, (
            f"Animated GIF {str(label)[:120]!r} is too large to animate "
            f"({frames} frames of {width}x{height}; the limit is {MAX_GIF_FRAMES} frames, "
            f"{MAX_GIF_DIMENSION} a side and {MAX_GIF_TOTAL_PIXELS} pixels in all): "
            f"showing its first frame only"))
        return 1
    return frames


def frame_budget(image, label="") -> int:
    """:func:`frames_allowed` for the open PIL *image*; reads headers only."""
    width, height = image.size
    check_texture_size(width, height, label)      # before counting frames
    return frames_allowed(width, height, getattr(image, "n_frames", 1), label)


#: Shortest frame time honoured: browsers treat 0-10 ms GIF delays as 100 ms
#: for the same reason (a 0 delay would spin).
MIN_FRAME_SECONDS = 0.02
DEFAULT_FRAME_SECONDS = 0.1


def _source(path):
    """What Pillow opens for *path*: the file, or the archived asset."""
    from engine.asset_archives import asset_source
    source = asset_source(path)
    return path if source is None else source


def is_animated_gif(path) -> bool:
    """True for a GIF with more than one frame."""
    if not str(path).lower().endswith(".gif"):
        return False
    try:
        from PIL import Image
        with Image.open(_source(path)) as image:
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
    are fitted to it, so every frame can be uploaded into one texture. A GIF
    over the animation budget (:func:`frame_budget`) yields its first frame
    only; one too large to be a texture raises :class:`TextureTooLarge`.
    """
    from PIL import Image, ImageSequence
    images, seconds = [], []
    with Image.open(_source(path)) as gif:
        allowed = frame_budget(gif, path)
        for frame in ImageSequence.Iterator(gif):
            if len(images) >= allowed:
                break
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
