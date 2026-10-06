"""Materials: shaders and textures.

Shader loading and compilation (including the instanced variants and their
uniform preloads), texture loading and caching, the animated FIRE/ORB effect
frames, the sprite texture array, model material textures, and terrain shader
and texture binding.
"""

import io
import os
import re

import numpy as np
import OpenGL.GL as gl
from OpenGL.GL.shaders import compileProgram, compileShader
from PyQt5.QtCore import QByteArray, QBuffer, QIODevice
from PyQt5.QtGui import QImage, QImageReader

from engine import shaders
from engine.shaders import DEFAULT_SHADERS
from engine.sprite_layers import SpriteLayers

_RENDERER_PREFIX = "\x1b[38;2;240;128;0m[Renderer]\x1b[0m"


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


class MaterialsMixin:
    """Shaders and textures of :class:`engine.renderer.Renderer`."""

    # --------------------------------------------------------------------------
    # Platform detection
    # --------------------------------------------------------------------------
    @staticmethod
    def _detect_lowpower_platform():
        """Whether this machine wants the low-power lighting shaders.

        Delegates to :func:`engine.shaders.detect_low_power_arm`, which is where
        the rule lives now — the renderer and the Settings window used to detect
        this separately and could reach different answers about one machine.
        """
        return shaders.detect_low_power_arm()[0]

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

    def _compile_common_shaders(self):
        """Compile the shaders every pass shares."""
        try:
            # simple (for grid, outlines, lines)
            vs_src = self._shader_source('simple.vert')
            fs_src = self._shader_source('simple.frag')
            self.shaders['simple'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['simple'] = UniformCache(self.shaders['simple'])
            self.uniforms['simple'].preload(['projection', 'view', 'model', 'color', 'alpha'])

            # sprite (billboards)
            vs_src = self._shader_source('sprite.vert')
            fs_src = self._shader_source('sprite.frag')
            self.shaders['sprite'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['sprite'] = UniformCache(self.shaders['sprite'])
            self.uniforms['sprite'].preload(['projection', 'view', 'sprite_texture', 'sprite_pos_world', 'sprite_size'])
            self.uniforms['sprite'].preload(self.ENV_UNIFORMS)

            # depth_cube – renders scene depth into a point light's cube-map for
            # omnidirectional shadow mapping (replaces the old projected shadows).
            vs_src = self._shader_source('depth_cube.vert')
            fs_src = self._shader_source('depth_cube.frag')
            self.shaders['depth_cube'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['depth_cube'] = UniformCache(self.shaders['depth_cube'])
            self.uniforms['depth_cube'].preload(['model', 'lightSpaceMatrix', 'lightPos', 'far_plane'])

            # water
            vs_src = self._shader_source('water.vert')
            fs_src = self._shader_source('water.frag')
            self.shaders['water'] = self.shader_loader.compile_from_source(vs_src, fs_src)
            self.uniforms['water'] = UniformCache(self.shaders['water'])
            self._preload_water_uniforms()

            # glass
            # Glass is an optional visual effect. A driver/compiler rejection here
            # must not abort common shader initialization and take the whole world
            # renderer down with it; the glass pass already skips itself when no
            # glass program is registered.
            try:
                vs_src = self._shader_source('glass.vert')
                fs_src = self._shader_source('glass.frag')
                glass_program = self.shader_loader.compile_from_source(vs_src, fs_src)
                self.shaders['glass'] = glass_program
                self.uniforms['glass'] = UniformCache(glass_program)
                self.uniforms['glass'].preload([
                    'projection', 'view', 'model', 'viewPos', 'waterColor',
                    'distortionStrength', 'fresnelIntensity', 'glassOpacity',
                    'refractionIndex', 'roughness', 'normalMatrix',
                    'sceneColor', 'screenSize'
                ])
                self.uniforms['glass'].preload(self.ENV_UNIFORMS)
            except Exception as e:
                self.shaders.pop('glass', None)
                self.uniforms.pop('glass', None)
                print(f"Glass shader unavailable; continuing without glass: {e}")
            # fog – use ARM‑optimised fragment shader (works everywhere)
            fog_vert = self._shader_source('fog.vert')
            fog_frag = self._shader_source('fog_arm.frag') or self._shader_source('fog.frag')
            self.shaders['fog'] = self.shader_loader.compile_from_source(fog_vert, fog_frag)
            self.uniforms['fog'] = UniformCache(self.shaders['fog'])
            self._preload_fog_uniforms()

            # terrain
            try:
                terrain_vs = compileShader(DEFAULT_SHADERS['terrain.vert'], gl.GL_VERTEX_SHADER)
                terrain_fs = compileShader(
                    shaders.light_ubo_source(DEFAULT_SHADERS['terrain.frag']),
                    gl.GL_FRAGMENT_SHADER,
                )
                terrain_program = compileProgram(terrain_vs, terrain_fs, validate=False)
                self.shaders['terrain'] = terrain_program
                self.uniforms['terrain'] = UniformCache(terrain_program)
                self.uniforms['terrain'].preload([
                    'projection', 'view', 'active_lights',
                    'texGrass', 'texRock', 'texSand', 'texSnow',
                ])
                self.uniforms['terrain'].preload(self.ENV_UNIFORMS)
                print("Terrain shader loaded")
            except Exception as e:
                print(f"Terrain shader error: {e}")
                self.shaders['terrain'] = None

            # lit and textured shaders (needed for forward fallback in Deferred)
            if self.lowpower_mode:
                self._compile_arm_shaders()
            else:
                self._compile_standard_shaders()

            self._configure_light_ubo_programs()
            print("Base renderer shaders compiled successfully.")
        except Exception as e:
            print(f"FATAL: Shader Error in Renderer: {e}")
            self._shader_init_failed = True

    def _compile_arm_shaders(self):
        lit_vert = DEFAULT_SHADERS.get('lit_arm.vert', '')
        lit_frag = DEFAULT_SHADERS.get('lit_arm.frag', '')
        lit_shader = self.shader_loader.compile_from_source(lit_vert, lit_frag)
        self.shaders['lit'] = lit_shader
        self.uniforms['lit'] = UniformCache(lit_shader)
        self._preload_lit_uniforms('lit')
        self.uniforms['lit'].preload(['normalMatrix'])

        tex_vert = DEFAULT_SHADERS.get('textured_arm.vert', '')
        tex_frag = DEFAULT_SHADERS.get('textured_arm.frag', '')
        tex_shader = self.shader_loader.compile_from_source(tex_vert, tex_frag)
        self.shaders['textured'] = tex_shader
        self.uniforms['textured'] = UniformCache(tex_shader)
        self._preload_lit_uniforms('textured')
        self.uniforms['textured'].preload(['texture_diffuse', 'tex_scale', 'tex_angle', 'tex_shift', 'normalMatrix'])
        self._compile_instanced_model_shaders(lit_vert, lit_frag, tex_vert, tex_frag)
        self._compile_instanced_brush_shader(tex_vert, tex_frag)
        self._compile_instanced_lit_brush_shader(lit_vert, lit_frag)
        self._compile_instanced_depth_shader()
        self._compile_instanced_sprite_shader()
        self._compile_instanced_effect_shader()

    def _compile_standard_shaders(self):
        lit_shader = self.shader_loader.compile_shader_program('lit.vert', 'lit.frag')
        self.shaders['lit'] = lit_shader
        self.uniforms['lit'] = UniformCache(lit_shader)
        self._preload_lit_uniforms('lit')
        self.uniforms['lit'].preload(['normalMatrix'])

        tex_shader = self.shader_loader.compile_shader_program('textured.vert', 'textured.frag')
        self.shaders['textured'] = tex_shader
        self.uniforms['textured'] = UniformCache(tex_shader)
        self._preload_lit_uniforms('textured')
        self.uniforms['textured'].preload(['texture_diffuse', 'tex_scale', 'tex_angle', 'tex_shift', 'normalMatrix'])
        lit_vert = DEFAULT_SHADERS.get('lit.vert', '')
        lit_frag = DEFAULT_SHADERS.get('lit.frag', '')
        tex_vert = DEFAULT_SHADERS.get('textured.vert', '')
        tex_frag = DEFAULT_SHADERS.get('textured.frag', '')
        self._compile_instanced_model_shaders(lit_vert, lit_frag, tex_vert, tex_frag)
        self._compile_instanced_brush_shader(tex_vert, tex_frag)
        self._compile_instanced_lit_brush_shader(lit_vert, lit_frag)
        self._compile_instanced_depth_shader()
        self._compile_instanced_sprite_shader()
        self._compile_instanced_effect_shader()

    #: Floats per brush-face instance: a mat4 model matrix, a mat3 normal
    #: matrix padded to three vec4 (with the face's UV rotation tucked into the
    #: first spare w), and one vec4 of UV scale and shift.  Eight vec4s, so
    #: attribute locations 3..10 -- 0..2 are the cube's own position, normal and
    #: texture coordinate.
    BRUSH_INSTANCE_FLOATS = 32

    #: Attribute locations 3..10 carry one instance of a brush draw run, and
    #: mean the same thing for every pass that uses them:
    #:
    #:   3..6   mat4 model
    #:   7..9   mat3 normal matrix, padded to vec4; ``iNormal0.w`` is a spare
    #:          scalar a pass may use (the textured pass puts the face's UV
    #:          rotation there)
    #:   10     vec4 payload, whose meaning is the pass's own (UV scale and
    #:          shift for textured, colour and alpha for lit)
    #:
    #: One layout, one buffer and one VAO serve both passes, which is what
    #: makes "a run describes the invariant GPU state, the instance array
    #: describes everything that varies within it" a property of the renderer
    #: rather than of one pass.
    BRUSH_INSTANCE_ATTRS = """layout (location = 3) in vec4 iModel0;
layout (location = 4) in vec4 iModel1;
layout (location = 5) in vec4 iModel2;
layout (location = 6) in vec4 iModel3;
layout (location = 7) in vec4 iNormal0;
layout (location = 8) in vec4 iNormal1;
layout (location = 9) in vec4 iNormal2;
layout (location = 10) in vec4 iPayload;

"""

    #: Vertex-shader uniforms the instanced variants drop, because the value is
    #: per instance now rather than per draw.
    _INSTANCED_VERT_DROP = ('uniform mat4 model;', 'uniform mat3 normalMatrix;',
                            'uniform vec2 tex_scale', 'uniform float tex_angle',
                            'uniform vec2 tex_shift')

    def _instanced_vertex_source(self, vert, preamble, extra_out=''):
        """A vertex shader rewritten to take its transform from instance data.

        Shared by the textured and lit brush passes: both start from the
        ordinary shader and differ only in what they pull out of the payload,
        so neither can drift from the pass it accelerates.
        """
        kept = [line for line in vert.splitlines()
                if not line.strip().startswith(self._INSTANCED_VERT_DROP)]
        source = '\n'.join(kept)
        if 'out vec3 FragPos;' not in source or 'void main() {' not in source:
            return None
        source = source.replace(
            'out vec3 FragPos;',
            self.BRUSH_INSTANCE_ATTRS + extra_out + 'out vec3 FragPos;', 1)
        source = source.replace(
            'void main() {',
            'void main() {\n'
            '    mat4 instanceModel = mat4(iModel0, iModel1, iModel2, iModel3);\n'
            '    mat3 instanceNormal = mat3(iNormal0.xyz, iNormal1.xyz, iNormal2.xyz);\n'
            + preamble, 1)
        source = source.replace('model * vec4(aPos, 1.0)',
                                'instanceModel * vec4(aPos, 1.0)')
        source = source.replace('normalMatrix * aNormal',
                                'instanceNormal * aNormal')
        return source

    def _register_instanced_shader(self, name, vertex_source, fragment_source,
                                   extra_uniforms=()):
        """Compile one instanced brush program, or leave it absent.

        Absence is a supported state, not a failure: a driver that rejects the
        attribute interface simply keeps the per-object path, which every pass
        retains.
        """
        if not vertex_source or not fragment_source:
            return False
        try:
            program = self.shader_loader.compile_from_source(vertex_source,
                                                             fragment_source)
        except Exception as exc:
            print(f'[Renderer] {name} instancing disabled: {exc}')
            return False
        self.shaders[name] = program
        self.uniforms[name] = UniformCache(program)
        self._preload_lit_uniforms(name)
        if extra_uniforms:
            self.uniforms[name].preload(list(extra_uniforms))
        return True

    def _compile_instanced_depth_shader(self):
        """Compile the shadow depth shader with an instanced model matrix.

        The depth pass is the purest run/instance split in the renderer: the
        only thing that varies per caster is its model matrix, and the only
        thing that varies per run is the cube face's ``lightSpaceMatrix``. So
        the matrix becomes instance data and the face stays a uniform, and one
        light's six faces cost six draws instead of six times its caster count.

        The vertex shader takes the same instance attributes as the brush
        passes, so the same buffer and the same VAO serve it; the normal and
        payload slots go unused here, which costs a little upload bandwidth and
        buys one layout for the whole renderer.
        """
        vert = self._shader_source('depth_cube.vert')
        frag = self._shader_source('depth_cube.frag')
        if not vert or not frag:
            return
        vertex = self._instanced_vertex_source(vert, preamble='')
        if vertex is None:
            return
        if self._register_instanced_shader(
                'depth_cube_instanced', vertex, frag,
                extra_uniforms=['lightSpaceMatrix', 'lightPos', 'far_plane']):
            print(f'{_RENDERER_PREFIX} Shadow depth instancing shader compiled successfully.')

    #: Per-instance attributes: centre, world size, optional locked yaw, opacity.
    #: Per-instance attributes: centre (3), world size (2), locked yaw,
    #: opacity, and the texture-array layer the entity pass samples.  Eight
    #: floats; a pass that does not use a column still writes it, because the
    #: staging array is shared and a stale value is somebody else's sprite.
    SPRITE_INSTANCE_FLOATS = 8

    def _compile_instanced_sprite_shader(self):
        """Compile the billboard shader with its centre and size per instance.

        The sprite pass was the last one submitting per object: one
        ``glDrawArrays`` and two uniform uploads for every billboard in range,
        where the brush passes had long since collapsed to one draw per state
        run.  Instancing it is the same trade they made -- what cannot vary
        within a draw (the texture) stays a bind, and what does (where the
        billboard is and how big) becomes instance data.

        Derived from ``sprite.vert`` by rewriting the two uniforms into
        attributes rather than written out again, so the billboard's
        camera-facing maths cannot drift from the path it accelerates.
        """
        vert = self._shader_source('sprite.vert')
        frag = self._shader_source('sprite.frag')
        if not vert or not frag:
            return
        kept = [line for line in vert.splitlines()
                if not line.strip().startswith(('uniform vec3 sprite_pos_world',
                                                'uniform vec2 sprite_size'))]
        source = '\n'.join(kept)
        if 'out vec2 TexCoords;' not in source:
            return
        # sprite.vert already declares the fixed-facing input for the
        # non-instanced path. Keep that declaration and only inject the
        # position/size instance inputs that the instanced rewrite needs.
        instance_decls = (
            'layout (location = 1) in vec3 iSpritePos;\n'
            'layout (location = 2) in vec2 iSpriteSize;\n'
            'layout (location = 4) in float iSpriteAlpha;\n'
        )
        if 'iSpritePos' not in source:
            source = source.replace(
                'out vec2 TexCoords;',
                instance_decls + 'out vec2 TexCoords;', 1)
        source = source.replace('sprite_pos_world', 'iSpritePos')
        source = source.replace('sprite_size.x', 'iSpriteSize.x')
        source = source.replace('sprite_size.y', 'iSpriteSize.y')
        source = source.replace('sprite_fixed_yaw', 'iSpriteFixedYaw')
        if 'iSpritePos' not in source or 'iSpriteSize.x' not in source:
            # The shader did not look the way this rewrite assumes; leaving the
            # program absent keeps the per-sprite path, which every caller has.
            return
        source = source.replace(
            'out vec2 TexCoords;',
            'out vec2 TexCoords;\nflat out float InstanceAlpha;',
            1,
        )
        source = source.replace(
            'void main() {',
            'void main() {\n    InstanceAlpha = iSpriteAlpha;',
            1,
        )
        frag = frag.replace(
            'in highp vec3 FragPos;',
            'in highp vec3 FragPos;\nflat in float InstanceAlpha;',
            1,
        )
        frag = frag.replace(
            'FragColor = vec4(applyFog(texColor.rgb, FragPos), texColor.a);',
            'FragColor = vec4(applyFog(texColor.rgb, FragPos), texColor.a * InstanceAlpha);',
            1,
        )
        if self._register_instanced_shader('sprite_instanced', source, frag,
                                           extra_uniforms=['projection', 'view',
                                                           'sprite_texture',
                                                           'use_fixed_facing']):
            print(f'{_RENDERER_PREFIX} Sprite instancing shader compiled successfully.')
        self._compile_layered_sprite_shader(source, frag)

    def _compile_layered_sprite_shader(self, vertex, fragment):
        """The instanced billboard shader, sampling a texture-array layer.

        Derived from the instanced shader by rewriting its sampler, as that one
        is derived from ``sprite.vert``: the billboard maths, fog and alpha
        handling exist once.  The layer arrives as instance data, which is what
        lets the entity sprite pass be one draw in depth order (see
        :mod:`engine.sprite_layers`).
        """
        if ('uniform sampler2D sprite_texture;' not in fragment
                or 'texture(sprite_texture, TexCoords)' not in fragment
                or 'void main() {' not in vertex):
            return
        vertex = vertex.replace(
            'out vec2 TexCoords;',
            'layout (location = 5) in float iSpriteLayer;\n'
            'flat out float InstanceLayer;\nout vec2 TexCoords;', 1)
        vertex = vertex.replace(
            'void main() {', 'void main() {\n    InstanceLayer = iSpriteLayer;', 1)
        fragment = fragment.replace(
            'uniform sampler2D sprite_texture;',
            'uniform sampler2DArray sprite_layers;\nflat in float InstanceLayer;', 1)
        fragment = fragment.replace(
            'texture(sprite_texture, TexCoords)',
            'texture(sprite_layers, vec3(TexCoords, InstanceLayer))', 1)
        if self._register_instanced_shader('sprite_layered', vertex, fragment,
                                           extra_uniforms=['projection', 'view',
                                                           'sprite_layers',
                                                           'use_fixed_facing']):
            print(f'{_RENDERER_PREFIX} Layered sprite shader compiled successfully.')

    # One Effect row expands into deterministic virtual flame cards.
    EFFECT_INSTANCE_FLOATS = 16

    FIRE_VIRTUAL_CARDS = 20

    def _compile_instanced_effect_shader(self):
        """Compile the single procedural FIRE/EXPLOSION instance shader."""
        vert = DEFAULT_SHADERS.get('effect.vert', '')
        frag = DEFAULT_SHADERS.get('effect.frag', '')
        if not vert or not frag:
            return
        if self._register_instanced_shader(
            'effect_instanced',
            vert,
            frag,
            extra_uniforms=['projection', 'view', 'explosion_texture'],
        ):
            print(f'{_RENDERER_PREFIX} Effect instancing shader compiled successfully.')

    def _compile_instanced_lit_brush_shader(self, lit_vert, lit_frag):
        """Compile the flat-shaded brush shader with instanced colour.

        The lit pass had no texture to batch by, so every brush was its own
        draw carrying four uniform uploads -- model, normal, colour, alpha.
        Colour and alpha are read in the *fragment* stage, so instancing them
        means carrying them across as a varying; the rest of the lighting,
        shadowing and fog code is untouched.
        """
        if not lit_vert or not lit_frag:
            return
        vertex = self._instanced_vertex_source(
            lit_vert,
            preamble='    vInstanceColor = iPayload;\n',
            extra_out='out vec4 vInstanceColor;\n')
        if vertex is None:
            return

        kept = [line for line in lit_frag.splitlines()
                if not line.strip().startswith(('uniform vec3 object_color;',
                                                'uniform float alpha;'))]
        fragment = '\n'.join(kept)
        if 'in vec3 Normal;' not in fragment:
            return
        fragment = fragment.replace('in vec3 Normal;',
                                    'in vec3 Normal;\nin vec4 vInstanceColor;', 1)
        fragment, colours = re.subn(r'\bobject_color\b', 'vInstanceColor.rgb',
                                    fragment)
        fragment, alphas = re.subn(r'\balpha\b', 'vInstanceColor.a', fragment)
        if not colours or not alphas:
            # The shader did not look the way this rewrite assumes; leaving the
            # program absent keeps the per-brush path rather than compiling
            # something subtly wrong.
            return
        if self._register_instanced_shader('lit_brush_instanced', vertex,
                                           fragment):
            print(f'{_RENDERER_PREFIX} Lit brush instancing shader compiled successfully.')

    def _compile_instanced_brush_shader(self, tex_vert, tex_frag):
        """Compile the textured-brush shader with per-face instanced attributes.

        The per-face state the pass used to upload as uniforms -- model matrix,
        normal matrix, UV scale, rotation and shift -- becomes instance data,
        so every face sharing a texture and a cube face index is one
        ``glDrawArraysInstanced`` instead of one ``glDrawArrays`` and three or
        four ``glUniform`` calls each.

        The UV scale and shift ride in the payload vec4 and the rotation in the
        spare ``iNormal0.w``; declaring locals of the shader's original uniform
        names leaves the UV rotate/scale/shift maths in the body byte for byte
        what the non-instanced path runs.  Both the desktop and ARM variants
        have the same vertex interface, so one derivation serves both.
        """
        vertex = self._instanced_vertex_source(
            tex_vert,
            preamble=('    vec2 tex_scale = iPayload.xy;\n'
                      '    vec2 tex_shift = iPayload.zw;\n'
                      '    float tex_angle = iNormal0.w;\n'))
        if vertex is None:
            return
        if self._register_instanced_shader('brush_instanced', vertex, tex_frag,
                                           extra_uniforms=['texture_diffuse']):
            print(f'{_RENDERER_PREFIX} Brush face instancing shader compiled successfully.')

    def _compile_instanced_model_shaders(self, lit_vert, lit_frag, tex_vert, tex_frag):
        """Compile GL 3.3 model shaders whose transforms come from instanced attributes."""
        instance_attrs = """layout (location = 3) in vec4 iModel0;
layout (location = 4) in vec4 iModel1;
layout (location = 5) in vec4 iModel2;
layout (location = 6) in vec4 iModel3;
layout (location = 7) in vec4 iNormal0;
layout (location = 8) in vec4 iNormal1;
layout (location = 9) in vec4 iNormal2;
layout (location = 10) in float iInstanceAlpha;

"""

        def make_vertex(source):
            if not source:
                raise ValueError('missing model vertex shader source')
            source = source.replace('uniform mat4 model;\n', '')
            source = source.replace('uniform mat3 normalMatrix;\n', '')
            if 'out vec3 FragPos;' not in source:
                raise ValueError('unexpected model vertex shader interface')
            source = source.replace(
                'out vec3 FragPos;',
                instance_attrs + 'flat out float InstanceAlpha;\nout vec3 FragPos;',
                1,
            )
            source = source.replace(
                'void main() {',
                'void main() {\n'
                '    mat4 instanceModel = mat4(iModel0, iModel1, iModel2, iModel3);\n'
                '    mat3 instanceNormal = mat3(iNormal0.xyz, iNormal1.xyz, iNormal2.xyz);\n'
                '    InstanceAlpha = iInstanceAlpha;\n',
                1,
            )
            source = source.replace('model * vec4(aPos, 1.0)', 'instanceModel * vec4(aPos, 1.0)')
            source = source.replace('normalMatrix * aNormal', 'instanceNormal * aNormal')
            return source
        lit_instance_frag = lit_frag.replace(
            'out vec4 FragColor;',
            'out vec4 FragColor;\nflat in float InstanceAlpha;',
            1,
        ).replace(
            'FragColor = vec4(applyFog(result, FragPos), alpha);',
            'FragColor = vec4(applyFog(result, FragPos), alpha * InstanceAlpha);',
            1,
        )
        textured_instance_frag = tex_frag.replace(
            'out vec4 FragColor;',
            'out vec4 FragColor;\nflat in float InstanceAlpha;',
            1,
        ).replace(
            'uniform sampler2D texture_diffuse;',
            'uniform sampler2D texture_diffuse;\nuniform float alpha;',
            1,
        ).replace(
            'FragColor = vec4(applyFog(result, FragPos), texColor.a);',
            'FragColor = vec4(applyFog(result, FragPos), texColor.a * alpha * InstanceAlpha);',
            1,
        )
        try:
            self.shaders['lit_instanced'] = self.shader_loader.compile_from_source(
                make_vertex(lit_vert), lit_instance_frag)
            self.uniforms['lit_instanced'] = UniformCache(self.shaders['lit_instanced'])
            self._preload_lit_uniforms('lit_instanced')

            self.shaders['textured_instanced'] = self.shader_loader.compile_from_source(
                make_vertex(tex_vert), textured_instance_frag)
            self.uniforms['textured_instanced'] = UniformCache(self.shaders['textured_instanced'])
            self._preload_lit_uniforms('textured_instanced')
            self.uniforms['textured_instanced'].preload(
                ['texture_diffuse', 'tex_scale', 'tex_angle', 'tex_shift', 'normalMatrix'])
            print(f'{_RENDERER_PREFIX} GPU model instancing shaders compiled successfully.')
        except Exception as exc:
            # The ordinary model shaders remain authoritative if an older/quirky
            # driver rejects the instanced attribute interface.
            for name in ('lit_instanced', 'textured_instanced'):
                self.uniforms.pop(name, None)
                program = self.shaders.pop(name, None)
                if program:
                    try:
                        gl.glDeleteProgram(program)
                    except Exception:
                        pass
            print(f'[Renderer] GPU model instancing disabled: {exc}')

    def _preload_lit_uniforms(self, shader_name):
        uniforms = self.uniforms[shader_name]
        uniforms.preload(['projection', 'view', 'model', 'object_color', 'alpha', 'active_lights'])
        uniforms.preload(self.ENV_UNIFORMS)

    def _preload_water_uniforms(self):
        uniforms = self.uniforms['water']
        uniforms.preload([
            'projection', 'view', 'model', 'time', 'viewPos',
            'normalMap', 'sceneColor',
            'screenSize', 'waterOpacity', 'waterReflectivity',
            'waterTint', 'distortionStrength', 'refractionIndex',
            'roughness', 'fresnelIntensity', 'normalMatrix',
            'waveAmp', 'brushSize',
            'sceneDepth', 'hasSceneDepth', 'ssrEnabled', 'invProjection',
        ])
        uniforms.preload(self.ENV_UNIFORMS)

    def _preload_fog_uniforms(self):
        uniforms = self.uniforms['fog']
        uniforms.preload(['projection', 'view', 'model', 'viewPos', 'time', 'noiseTexture',
                          'density', 'fogColor', 'noiseScale', 'object_color', 'alpha', 'inverseModel'])

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
            return tex_id
        except Exception as e:
            print(f"Error loading texture '{texture_name}': {e}")
            return self.load_texture('default.png', 'textures')

    def preload_level_textures(self, brushes):
        texture_set = set()
        for brush in brushes:
            for face_tex in brush.get('textures', {}).values():
                if face_tex and face_tex != 'caulk.jpg':
                    texture_set.add(face_tex)
            # Angled brushes can carry per-plane textures (e.g. on cut faces).
            geo = brush.get('geometry')
            if geo:
                for plane in geo.get('planes', []):
                    tex = plane.get('texture')
                    if tex and tex != 'caulk.jpg':
                        texture_set.add(tex)
        for tex_name in texture_set:
            self.load_texture(tex_name, 'textures')

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

    # --------------------------------------------------------------------------
    # Terrain
    # --------------------------------------------------------------------------
    def setup_terrain_shader(self, terrain):
        if 'terrain' not in self.shaders or not self.shaders['terrain']:
            return
        terrain.shader_program = self.shaders['terrain']
        terrain.uniforms = {
            'projection': self.uniforms['terrain']['projection'],
            'view': self.uniforms['terrain']['view'],
            'active_lights': self.uniforms['terrain']['active_lights'],
            'texGrass': self.uniforms['terrain']['texGrass'],
            'texRock': self.uniforms['terrain']['texRock'],
            'texSand': self.uniforms['terrain']['texSand'],
            'texSnow': self.uniforms['terrain']['texSnow'],
        }
        for i in range(self.MAX_LIGHTS):
            terrain.uniforms[f'lights[{i}].position'] = self.uniforms['terrain'][f'lights[{i}].position']
            terrain.uniforms[f'lights[{i}].color'] = self.uniforms['terrain'][f'lights[{i}].color']
            terrain.uniforms[f'lights[{i}].intensity'] = self.uniforms['terrain'][f'lights[{i}].intensity']
            terrain.uniforms[f'lights[{i}].radius'] = self.uniforms['terrain'][f'lights[{i}].radius']

    def _ensure_terrain_textures(self, terrain):
        mappings = [('grass_tex', 'grass.jpg'), ('rock_tex', 'rock.jpg'),
                    ('sand_tex', 'sand.jpg'), ('snow_tex', 'snow.jpg')]
        self.load_texture('default.png', 'textures')
        for attr, filename in mappings:
            current_id = getattr(terrain, attr, 0)
            if not current_id or current_id == -1:
                new_id = self.load_texture(filename, 'textures/terrain')
                setattr(terrain, attr, new_id)

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

    def _sprite_layer_array(self):
        """The entity sprite texture array, created on first use."""
        if self._sprite_layers is None:
            # Layers are square and shared, so their size is a memory
            # decision: 512 holds every stock sprite at full resolution;
            # low-power mode halves the edge and quarters the footprint.
            self._sprite_layers = SpriteLayers(
                max_size=256 if getattr(self, 'lowpower_mode', False) else 512)
        return self._sprite_layers

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
