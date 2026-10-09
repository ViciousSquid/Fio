"""Renderer-independent resources: textures, models, effect frames, shaders.

The texture cache behind the public ``load_texture``, the sprite texture table
the host provides, OBJ/GLB model loading, the decoded FIRE/ORB effect frames,
and the shader loading helpers (:class:`ShaderLoader`, :class:`UniformCache`)
with the overlay shader the core overlays draw with.
"""

import io
import os
import time

import numpy as np
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader
from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
from PyQt5.QtGui import QImage, QImageReader

from engine import shaders
from engine.shaders import DEFAULT_SHADERS

# Try to import OBJ and GLB loaders
try:
    from engine.obj_loader import OBJ
except ImportError:
    OBJ = None

try:
    from engine.glb_loader import GLB
except ImportError:
    GLB = None

_RENDERER_PREFIX = "\x1b[38;2;240;128;0m[Renderer]\x1b[0m"


#: Seconds before a model path that failed to load is tried again.
_MODEL_RETRY_S = 5.0


# ---------- Utility classes ----------
class UniformCache:
    __slots__ = ('program', '_cache', 'shadow_slots')
    def __init__(self, shader_program):
        self.program = shader_program
        self._cache = {}
        # The shadowMaps[i] slots this program samples, once their sampler
        # units are assigned (see Renderer._bind_shadow_maps); None before.
        self.shadow_slots = None
    def __getitem__(self, name):
        loc = self._cache.get(name)
        if loc is None:
            loc = gl.glGetUniformLocation(self.program, name)
            self._cache[name] = loc
        return loc
    def preload(self, names):
        for name in names:
            if name not in self._cache:
                self._cache[name] = gl.glGetUniformLocation(self.program, name)
    def get(self, name, default=-1):
        return self._cache.get(name, default)


class ShaderLoader:
    def __init__(self, shader_dir='assets/shaders'):
        self.shader_dir = shader_dir
        if not os.path.exists(self.shader_dir):
            try:
                os.makedirs(self.shader_dir)
            except OSError:
                if os.path.exists('shaders'):
                    self.shader_dir = 'shaders'
        self._ensure_defaults()

    def _ensure_defaults(self):
        for filename, source in DEFAULT_SHADERS.items():
            filepath = os.path.join(self.shader_dir, filename)
            try:
                with open(filepath, 'w') as f:
                    f.write(source)
            except Exception as e:
                print(f"Error generating shader {filename}: {e}")

    def _read_source(self, filename):
        filepath = os.path.join(self.shader_dir, filename)
        if not os.path.exists(filepath):
            if os.path.exists(filename):
                filepath = filename
            else:
                raise FileNotFoundError(f"Shader file not found: {filename}")
        with open(filepath, 'r') as f:
            return f.read()

    def compile_shader_program(self, vertex_file, fragment_file, geometry_file=None):
        try:
            vertex_src = self._read_source(vertex_file)
            fragment_src = shaders.light_ubo_source(self._read_source(fragment_file))
            vs = compileShader(vertex_src, gl.GL_VERTEX_SHADER)
            fs = compileShader(fragment_src, gl.GL_FRAGMENT_SHADER)
            if geometry_file:
                geometry_src = self._read_source(geometry_file)
                gs = compileShader(geometry_src, gl.GL_GEOMETRY_SHADER)
                program = compileProgram(vs, fs, gs, validate=False)
            else:
                program = compileProgram(vs, fs, validate=False)
            return program
        except Exception as e:
            print(f"Error compiling shader ({vertex_file}, {fragment_file}): {e}")
            raise

    def compile_from_source(self, vertex_src, fragment_src):
        if not vertex_src:
            raise ValueError("empty vertex shader source")
        if not fragment_src:
            raise ValueError("empty fragment shader source")
        try:
            fragment_src = shaders.light_ubo_source(fragment_src)
            vs = compileShader(vertex_src, gl.GL_VERTEX_SHADER)
            fs = compileShader(fragment_src, gl.GL_FRAGMENT_SHADER)
            return compileProgram(vs, fs, validate=False)
        except Exception as e:
            print(f"Error compiling shader from source: {e}")
            raise


class ResourcesMixin:
    """Textures, models, effect frames and shader helpers."""

    # --------------------------------------------------------------------------
    # Shader compilation helpers
    # --------------------------------------------------------------------------
    def _shader_source(self, name):
        """Return an embedded shader or its loose asset-file fallback.

        A few larger shaders intentionally live only in assets/shaders rather
        than being duplicated in DEFAULT_SHADERS.  Never pass an empty string
        to the GL compiler just because a default entry is absent.
        """
        source = DEFAULT_SHADERS.get(name)
        if source:
            return source
        try:
            return self.shader_loader._read_source(name)
        except (FileNotFoundError, OSError):
            return ''

    # --------------------------------------------------------------------------
    # Texture management
    # --------------------------------------------------------------------------
    def load_texture(self, texture_name, subfolder):
        tex_cache_name = os.path.join(subfolder, texture_name)
        if tex_cache_name in self.texture_manager:
            return self.texture_manager[tex_cache_name]

        # Initialize texture dimensions storage
        if not hasattr(self, '_texture_dimensions'):
            self._texture_dimensions = {}

        if texture_name == 'default.png':
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            self._texture_dimensions[tex_cache_name] = (1, 1)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 1, 1, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                           (gl.GLubyte * 4)(255, 255, 255, 255))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            return tex_id

        if texture_name == 'caulk':
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            self._texture_dimensions[tex_cache_name] = (2, 2)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, 2, 2, 0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE,
                           (gl.GLubyte * 16)(255, 0, 255, 255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 0, 255, 255))
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_NEAREST)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_NEAREST)
            return tex_id

        texture_path = os.path.join('assets', subfolder, texture_name)
        if not os.path.exists(texture_path):
            return self.load_texture('default.png', 'textures')

        try:
            from PIL import Image
            from engine.animated_texture import cumulative, decode_gif_frames, is_animated_gif
            frames = None
            if is_animated_gif(texture_path):
                images, seconds = decode_gif_frames(texture_path)
                frames = [im.transpose(Image.FLIP_TOP_BOTTOM).tobytes() for im in images]
                img = images[0].transpose(Image.FLIP_TOP_BOTTOM)
            else:
                img = Image.open(texture_path).convert("RGBA")
                img = img.transpose(Image.FLIP_TOP_BOTTOM)
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            self._texture_dimensions[tex_cache_name] = (img.width, img.height)
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0,
                           gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img.tobytes())
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            if frames and len(frames) > 1:
                self._animated_textures()[int(tex_id)] = {
                    'size': (img.width, img.height), 'frames': frames,
                    'ends': cumulative(seconds), 'shown': 0}
            return tex_id
        except Exception as e:
            print(f"Error loading texture '{texture_name}': {e}")
            return self.load_texture('default.png', 'textures')

    # --------------------------------------------------------------------------
    # Animated (GIF) textures
    # --------------------------------------------------------------------------
    def _animated_textures(self):
        """``{GL texture name: animation}`` for every animated texture loaded."""
        table = getattr(self, '_animated_texture_table', None)
        if table is None:
            table = self._animated_texture_table = {}
        return table

    def animate_textures(self, clock: float) -> None:
        """Show each animated texture's frame for *clock* seconds of play.

        Optional for renderers; the host calls it before drawing a frame,
        with 0 outside Play (the first frame). A frame change re-uploads into
        the same texture, so nothing that holds the texture name notices.
        """
        table = getattr(self, '_animated_texture_table', None)
        if not table:
            return
        from engine.animated_texture import frame_at
        changed = False
        for tex_id, anim in table.items():
            index = frame_at(anim['ends'], clock)
            if index == anim['shown']:
                continue
            anim['shown'] = index
            width, height = anim['size']
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, width, height,
                               gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, anim['frames'][index])
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            changed = True
        if changed:
            gl.glBindTexture(gl.GL_TEXTURE_2D, 0)

    def _load_3d_texture(self, filepath, size=32):
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            if len(data) != size ** 3:
                return 0
            texture_id = gl.glGenTextures(1)
            gl.glBindTexture(gl.GL_TEXTURE_3D, texture_id)
            for param in [(gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT), (gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT),
                          (gl.GL_TEXTURE_WRAP_R, gl.GL_REPEAT), (gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                          (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)]:
                gl.glTexParameteri(gl.GL_TEXTURE_3D, *param)
            gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_R8, size, size, size, 0,
                            gl.GL_RED, gl.GL_UNSIGNED_BYTE, data)
            return texture_id
        except Exception:
            return 0

    # ------------------------------------------------------------------
    def _tex_cache_path(self, tex_name):
        """Return the ``textures/<name>`` cache key for *tex_name*, memoizing the
        os.path.join. Called for every drawn face every frame in play mode, so
        the join is done once per unique texture name and reused thereafter."""
        path = self._tex_path_cache.get(tex_name)
        if path is None:
            path = os.path.join('textures', tex_name)
            self._tex_path_cache[tex_name] = path
        return path

    def set_sprite_textures(self, textures):
        self.sprite_textures = textures

    def _texture_pixel_size(self, tex_name):
        """Pixel dimensions of a loaded texture, with the usual 128 fallback."""
        cache_name = os.path.join('textures', tex_name)
        return getattr(self, '_texture_dimensions', {}).get(cache_name, (128, 128))

    # --------------------------------------------------------------------------
    # FIRE animation textures
    # --------------------------------------------------------------------------
    def _fire_asset_bytes(self, asset_path):
        """Read an effect asset's bytes from the project directory."""
        disk_path = os.path.join(os.getcwd(), asset_path)
        if os.path.exists(disk_path):
            try:
                with open(disk_path, "rb") as handle:
                    return handle.read()
            except OSError:
                pass
        return None

    def _upload_fire_frame(self, cache_key, image):
        """Upload one already-decoded FIRE frame and return its GL texture id."""
        cached = self.texture_manager.get(cache_key)
        if cached:
            return int(cached)

        image = image.convertToFormat(QImage.Format_RGBA8888)
        image = image.mirrored(False, True)
        width, height = image.width(), image.height()
        bits = image.constBits()
        try:
            bits.setsize(image.sizeInBytes())
            pixels = bytes(bits)
        except AttributeError:
            pixels = image.bits().asstring(image.byteCount())

        tex_id = gl.glGenTextures(1)
        self.texture_manager[cache_key] = tex_id
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
        gl.glTexParameteri(
            gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE
        )
        gl.glTexParameteri(
            gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE
        )
        gl.glTexParameteri(
            gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR
        )
        gl.glTexParameteri(
            gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR
        )

        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        try:
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, width, height, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, pixels
            )
        finally:
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        return int(tex_id)

    def _load_fire_gif(self, asset_path):
        """Decode one FIRE GIF into persistent GL textures and frame timings.

        Pillow is already a Fio texture dependency and gives us the decoded
        animation frame sequence directly, including per-frame GIF durations.
        Qt's QImageReader remains the fallback for a malformed/unusual asset.
        """
        data = self._fire_asset_bytes(asset_path)
        if not data:
            print(f"{_RENDERER_PREFIX} FIRE texture not found: {asset_path}")
            return [], np.empty(0, dtype=np.float32)

        frames = []
        durations = []

        # Primary animated-GIF path. ImageSequence.Iterator handles GIF
        # disposal/compositing, so each uploaded texture is the complete frame
        # the player should actually display.
        try:
            from PIL import Image, ImageSequence

            with Image.open(io.BytesIO(data)) as gif:
                if getattr(gif, 'is_animated', False):
                    for frame_index, frame in enumerate(ImageSequence.Iterator(gif)):
                        rgba = frame.convert("RGBA")
                        qimage = QImage(
                            rgba.tobytes(),
                            rgba.width,
                            rgba.height,
                            rgba.width * 4,
                            QImage.Format_RGBA8888,
                        ).copy()
                        frames.append(
                            self._upload_fire_frame(
                                f"{asset_path}#frame={frame_index}",
                                qimage,
                            )
                        )
                        try:
                            delay_seconds = max(
                                float(frame.info.get("duration", 100)) / 1000.0,
                                0.01,
                            )
                        except (TypeError, ValueError):
                            delay_seconds = 0.1
                        durations.append(delay_seconds)
                else:
                    rgba = gif.convert("RGBA")
                    qimage = QImage(
                        rgba.tobytes(),
                        rgba.width,
                        rgba.height,
                        rgba.width * 4,
                        QImage.Format_RGBA8888,
                    ).copy()
                    frames.append(
                        self._upload_fire_frame(
                            f"{asset_path}#frame=0",
                            qimage,
                        )
                    )
                    durations.append(0.1)

            if frames:
                return frames, np.cumsum(
                    np.asarray(durations, dtype=np.float32),
                    dtype=np.float32,
                )
        except Exception as exc:
            print(
                f"{_RENDERER_PREFIX} Error decoding FIRE GIF "
                f"with Pillow '{asset_path}': {exc}"
            )

        # Qt fallback for assets Pillow cannot decode.
        try:
            payload = QByteArray(data)
            buffer = QBuffer()
            buffer.setData(payload)
            buffer.open(QIODevice.ReadOnly)
            reader = QImageReader(buffer, b"gif")
            reader.setDecideFormatFromContent(True)

            image_count = reader.imageCount()
            if image_count < 0:
                image_count = 0

            for frame_index in range(image_count):
                if not reader.jumpToImage(frame_index):
                    continue
                image = reader.read()
                if image.isNull():
                    continue
                frames.append(
                    self._upload_fire_frame(
                        f"{asset_path}#qt-frame={frame_index}",
                        image,
                    )
                )
                try:
                    delay_seconds = max(
                        float(reader.nextImageDelay()) / 1000.0,
                        0.01,
                    )
                except (TypeError, ValueError):
                    delay_seconds = 0.1
                durations.append(delay_seconds)

            buffer.close()

            if frames:
                return frames, np.cumsum(
                    np.asarray(durations, dtype=np.float32),
                    dtype=np.float32,
                )
        except Exception as exc:
            print(
                f"{_RENDERER_PREFIX} FIRE Qt fallback failed "
                f"'{asset_path}': {exc}"
            )

        return [], np.empty(0, dtype=np.float32)

    def _load_fire_effect_textures(self):
        """Load the five authored FIRE variants once after the GL context exists."""
        self.effect_fire_frames.clear()
        self.effect_fire_cumulative.clear()

        for variant in range(5):
            asset_path = (
                f"assets/textures/effects/fire{variant + 1:02d}.gif"
            )
            frames, cumulative = self._load_fire_gif(asset_path)

            # Optional variants may not exist yet.  Keep the selector usable
            # without inventing an asset: an absent variant falls back to FIRE 01.
            if not frames and variant != 0:
                frames = self.effect_fire_frames.get(0, ())
                cumulative = self.effect_fire_cumulative.get(
                    0, np.empty(0, dtype=np.float32)
                )

            self.effect_fire_frames[variant] = tuple(frames)
            self.effect_fire_cumulative[variant] = cumulative

    def _load_orb_effect_textures(self):
        """Load the five authored ORB variants once after the GL context exists."""
        self.effect_orb_frames.clear()
        self.effect_orb_cumulative.clear()

        for variant in range(5):
            asset_path = (
                f"assets/textures/effects/orb{variant + 1:02d}.gif"
            )
            frames, cumulative = self._load_fire_gif(asset_path)

            if not frames and variant != 0:
                frames = self.effect_orb_frames.get(0, ())
                cumulative = self.effect_orb_cumulative.get(
                    0, np.empty(0, dtype=np.float32)
                )

            self.effect_orb_frames[variant] = tuple(frames)
            self.effect_orb_cumulative[variant] = cumulative

    # --------------------------------------------------------------------------
    # Models
    # --------------------------------------------------------------------------
    def load_model(self, filename):
        """Load a 3D model (OBJ or GLB).

        The normal render-time case is an already-loaded model. Keep that path
        to a single dictionary lookup; path normalisation and filesystem work
        belong exclusively to cache misses.
        """
        if not filename:
            return None

        # HOT PATH: model_path values are normally identical strings frame to
        # frame, so this is the entire lookup on the common render path.
        model = self.loaded_models.get(filename)
        if model is not None:
            return model

        # A path that failed a moment ago is not retried every frame: every
        # draw, cull and shadow pass asks for it, and each retry was a
        # filesystem probe, a parse attempt and a log line. Retried after
        # _MODEL_RETRY_S, so a model added while Fio runs still appears.
        failed = self.__dict__.setdefault('_failed_models', {}).get(filename)
        if failed is not None and time.perf_counter() - failed < _MODEL_RETRY_S:
            return None

        # Cache miss only: normalise alternate slash/absolute-path spellings
        # so editor/package/file-dialog paths still collapse to one resource.
        original_filename = str(filename)
        normalized_filename = os.path.normpath(
            original_filename.replace('/', os.sep).replace('\\', os.sep)
        )
        cache_key = os.path.normcase(normalized_filename)

        model = self.loaded_models.get(cache_key)
        if model is not None:
            # Alias this exact authored path so subsequent frames stay on the
            # one-dictionary-lookup path above.
            self.loaded_models[filename] = model
            return model

        full_path = normalized_filename
        if not os.path.isabs(full_path):
            candidate = os.path.join('assets', 'models', full_path)
            if os.path.exists(candidate):
                full_path = candidate
            elif os.path.exists(original_filename):
                full_path = original_filename

        if not os.path.exists(full_path):
            return self._model_load_failed(filename)

        # Determine format by extension
        ext = os.path.splitext(full_path)[1].lower()

        if ext == '.glb':
            if GLB is None:
                print(f"[Renderer] GLB support not available (glb_loader not found)")
                return None
            model = GLB(full_path)
        elif ext in ('.obj', ''):
            if OBJ is None:
                print(f"[Renderer] OBJ support not available (obj_loader not found)")
                return None
            model = OBJ(full_path)
        else:
            print(f"[Renderer] Unsupported model format: {ext}")
            return None

        if model.is_loaded:
            self.loaded_models[cache_key] = model
            self.loaded_models[filename] = model
            self.__dict__.setdefault('_failed_models', {}).pop(filename, None)
            return model

        return self._model_load_failed(filename)

    def _model_load_failed(self, filename):
        """Remember a failed model path; report it the first time only."""
        failed = self.__dict__.setdefault('_failed_models', {})
        if filename not in failed:
            print(f"Failed to load model: {filename}")
        failed[filename] = time.perf_counter()
        return None

    def get_loaded_model(self, filename):
        """Return a model already loaded by this renderer without touching GL."""
        if not filename:
            return None

        model = self.loaded_models.get(filename)
        if model is not None:
            return model

        normalized_filename = os.path.normpath(
            str(filename).replace('/', os.sep).replace('\\', os.sep)
        )
        cache_key = os.path.normcase(normalized_filename)
        model = self.loaded_models.get(cache_key)
        if model is not None:
            self.loaded_models[filename] = model
        return model

    # --------------------------------------------------------------------------
    # Model and sprite textures
    # --------------------------------------------------------------------------
    def _model_texture_id(self, tex_name, material=None, manual=False):
        if not tex_name:
            return 0
        resolved_path = self._resolve_model_texture_path(
            material or {'texture': tex_name}, tex_name)
        use_direct = bool(
            resolved_path and os.path.exists(resolved_path) and
            (not manual or not resolved_path.startswith('assets'))
        )
        if not use_direct:
            return self.load_texture(tex_name, 'textures')
        tex_cache_name = f'model_tex:{resolved_path}'
        tex_id = self.texture_manager.get(tex_cache_name)
        if tex_id is not None:
            return tex_id
        try:
            from PIL import Image
            img = Image.open(resolved_path).convert('RGBA')
            img = img.transpose(Image.FLIP_TOP_BOTTOM)
            tex_id = gl.glGenTextures(1)
            self.texture_manager[tex_cache_name] = tex_id
            gl.glBindTexture(gl.GL_TEXTURE_2D, tex_id)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR_MIPMAP_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(
                gl.GL_TEXTURE_2D, 0, gl.GL_RGBA, img.width, img.height, 0,
                gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, img.tobytes())
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
            return tex_id
        except Exception as exc:
            print(f'[Renderer] Model texture load failed for {resolved_path}: {exc}')
            return self.load_texture(tex_name, 'textures')

    def _resolve_model_texture_path(self, material, texture_name):
        """
        Resolve a texture path from an MTL material.
        Checks in order:
          1. Relative to the MTL file's directory (correct for MTL references)
          2. assets/textures/ (global fallback)
          3. assets/models/ (legacy fallback)
        Returns the resolved path or None if not found.
        """
        if not texture_name:
            return None

        # Normalise separators from MTL files authored on another platform.
        texture_name = (
            str(texture_name)
            .strip()
            .strip('"')
            .replace('\\', os.sep)
            .replace('/', os.sep)
        )

        # 1. Try relative to the MTL file's directory (most correct for MTL refs)
        mtl_dir = material.get('mtl_dir', '')
        if mtl_dir:
            mtl_dir = (
                str(mtl_dir)
                .replace('\\', os.sep)
                .replace('/', os.sep)
            )
            resolved = os.path.normpath(os.path.join(mtl_dir, texture_name))
            if os.path.exists(resolved):
                return resolved

        # 2. Try assets/textures/ (global fallback)
        resolved = os.path.normpath(os.path.join('assets', 'textures', texture_name))
        if os.path.exists(resolved):
            return resolved

        # 3. Try assets/models/ (legacy fallback)
        resolved = os.path.normpath(os.path.join('assets', 'models', texture_name))
        if os.path.exists(resolved):
            return resolved

        # 4. Return as-is and let the loader handle errors
        return texture_name

    def _compile_overlay_shader(self):
        """Compile the ``simple`` program the core overlays draw with.

        Raises on failure, so a renderer can treat it like any other required
        program; ForwardRenderer compiles it first in ``_compile_common_shaders``.
        """
        # simple (for grid, outlines, lines)
        vs_src = self._shader_source('simple.vert')
        fs_src = self._shader_source('simple.frag')
        self.shaders['simple'] = self.shader_loader.compile_from_source(vs_src, fs_src)
        self.uniforms['simple'] = UniformCache(self.shaders['simple'])
        self.uniforms['simple'].preload(['projection', 'view', 'model', 'color', 'alpha'])
