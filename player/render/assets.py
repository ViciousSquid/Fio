"""Archive images and legal GLES uploads, shared by world effects and the HUD."""
import ctypes
from collections import OrderedDict
import io
import time
from PIL import Image
from engine.animated_texture import cumulative, frame_at, _frame_seconds


class GifFrames:
    """Keep the current full-resolution frame, rather than every decoded RGBA.

    Showcase fire alone has 98 384x512 frames (~74 MiB if all are retained).
    Pillow composites sequential seeks, preserving GIF disposal/transparency.
    """
    def __init__(self, raw):
        self.source = Image.open(io.BytesIO(raw))
        self.size = self.source.size
        self.seconds = []
        for index in range(self.source.n_frames):
            self.source.seek(index)
            self.seconds.append(_frame_seconds(self.source))
        self.source.seek(0)
        self.current = None
        self.image = None
        self.recent = OrderedDict()

    def __len__(self):
        return len(self.seconds)

    def __getitem__(self, index):
        if index in self.recent:
            self.recent.move_to_end(index)
            return self.recent[index]
        if index != self.current:
            self.source.seek(index)
            self.image = self.source.convert('RGBA')
            if self.image.size != self.size:
                self.image = self.image.resize(self.size)
            self.current = index
            self.recent[index] = self.image
            if len(self.recent) > 4:
                self.recent.popitem(last=False)
        return self.image


class Images:
    def __init__(self, package=None, embedded=None):
        self.package = package
        self.embedded = embedded if embedded is not None else {}
        self.cache = {}
        self.started = time.perf_counter()
        self.missing = set()

    def frames(self, name):
        if name not in self.cache:
            raw = self.embedded.get(name) or (self.package.read_asset(name) if self.package and name else None)
            if not name or str(name).split('/')[-1] == 'default.png':
                images, seconds = [Image.new('RGBA', (1, 1), 'white')], [.1]
            elif raw:
                if str(name).lower().endswith('.gif'):
                    images = GifFrames(raw)
                    seconds = images.seconds
                else:
                    with Image.open(io.BytesIO(raw)) as source:
                        images, seconds = [source.convert('RGBA')], [.1]
            else:
                if name not in self.missing:
                    print(f'[Assets] Missing world image: {name}', flush=True)
                    self.missing.add(name)
                image = Image.new('RGBA', (2, 2), 'magenta')
                image.putpixel((1, 0), (0, 0, 0, 255)); image.putpixel((0, 1), (0, 0, 0, 255))
                images, seconds = [image], [.1]
            self.cache[name] = images, cumulative(seconds)
        return self.cache[name]

    def size(self, name):
        return self.frames(name)[0][0].size

    def frame(self, name, clock=None, loop=True, phase=0):
        images, ends = self.frames(name)
        elapsed = time.perf_counter() - self.started if clock is None else max(0, clock)
        elapsed += phase * ends[-1]
        index = frame_at(ends, elapsed) if loop else (len(images)-1 if elapsed >= ends[-1] else frame_at(ends, elapsed))
        return index, images[index]


def upload_rgba(gl, image, *, update=False):
    """Bypass PyOpenGL's desktop-only pixel-store probes (invalid on GLES)."""
    from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D, glTexSubImage2D
    data = ctypes.create_string_buffer(image.tobytes())
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    if update:
        glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, image.width, image.height,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, ctypes.cast(data, ctypes.c_void_p))
    else:
        glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, image.width, image.height, 0,
                     gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, ctypes.cast(data, ctypes.c_void_p))
