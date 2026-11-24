"""The world's light: sun, sky and what the lit surfaces need from them.

:class:`Environment` is plain data -- where the sun is, what the sky is like,
how the frame is finished -- that a game (or a console command) sets on
``renderer.environment``. While it is off, which is how it starts, nothing in
this module draws or binds anything and the renderer is the one Fio always
had. Switched on, a Lit frame is drawn like this (``render_scene`` calls in):

1. the sun's shadow map: every shadow-casting brush and model, once, from
   the sun, redrawn only when one of them moves (:meth:`_env_sun_shadows`);
2. the frame goes to an HDR target (``postfx.py``);
3. the lit programs shade physically (``surface.py``); their uniforms and
   textures are set here (:meth:`_bind_surface`, :meth:`_bind_material_maps`);
4. the sky is this module's own: an analytic atmosphere under a layer of
   moving clouds (:meth:`_env_draw_sky`), also rendered into a small
   equirectangular environment map the surfaces reflect and take their sky
   light from;
5. ambient occlusion and reflections, then bloom, tone mapping and
   anti-aliasing (``postfx.py``).
"""

import json
import math
import os
import time

import glm
import numpy as np
import OpenGL.GL as gl
from engine.renderer.core import gl_state
from OpenGL.GL.shaders import compileProgram, compileShader

from engine import hd_graphics
from engine.constants import RENDER_MODE_LIT
from engine.renderer.core.uniform_rows import uniform_rows
from engine.renderer.hd_high import hdr, skylight, surface
from engine.renderer.hd_high.lighting import point_shadow_source
from ..core.raw_calls import uniform_matrix4

ENV_MAP_SIZE = (512, 256)
#: Frames between redraws of a procedural sky's environment map while its
#: clouds drift, and how far (in cloud features) they must have drifted.
ENV_DRIFT_FRAMES = 90
ENV_DRIFT_DISTANCE = 0.02

FULLSCREEN_VERT = """#version 330 core
out vec2 vNdc;
out vec2 vUV;
void main() {
    vec2 p = vec2(float((gl_VertexID << 1) & 2), float(gl_VertexID & 2)) * 2.0 - 1.0;
    vNdc = p;
    vUV = p * 0.5 + 0.5;
    gl_Position = vec4(p, 1.0, 1.0);
}
"""

SUN_DEPTH_FRAG = """#version 330 core
void main() {}
"""

#: The sky: Preetham's analytic daylight (the formulation three.js made
#: familiar) under a cloud layer lit by the sun. Mode 0 draws the background,
#: with the sun's disc, gamma-encoded like every colour in the HDR target;
#: mode 1 draws the equirectangular environment map, linear and without the
#: disc (the surfaces compute the sun's own highlight).
SKY_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vNdc;
in vec2 vUV;
uniform mat4 uInvProjection;
uniform mat4 uInvView;
uniform int uMode;
uniform vec3 uSunDir;
uniform vec3 uSunColor;
uniform vec4 uSkyParams;        // turbidity, rayleigh, mie coefficient, mie g
uniform vec4 uSkyParams2;       // sky gain, sun disc gain, cloud brightness, unused
uniform vec3 uGroundColor;
uniform highp vec3 uCamPos;
uniform sampler2D uSource;      // mode 2: a display-referred equirectangular image
uniform vec4 uSourceParams;     // x gain on its linear colour, y 1: flip v
""" + surface.ENV_GLSL + surface.CLOUD_GLSL + """
const float E_CUT = 1.6110731556870734;
const vec3 TOTAL_RAYLEIGH = vec3(5.804542996261093E-6, 1.3562911419845635E-5, 3.0265902468824876E-5);
const vec3 MIE_CONST = vec3(1.8399918514433978E14, 2.7798023919660528E14, 4.0790479543861094E14);

vec3 fioAtmosphere(vec3 dir) {
    float sunE = 1000.0 * max(0.0, 1.0 - exp(-((E_CUT - acos(clamp(uSunDir.y, -1.0, 1.0))) / 1.5)));
    float sunfade = 1.0 - clamp(1.0 - exp(uSunDir.y), 0.0, 1.0);
    vec3 betaR = TOTAL_RAYLEIGH * (uSkyParams.y - (1.0 - sunfade));
    vec3 betaM = 0.434 * (0.2 * uSkyParams.x) * 10E-18 * MIE_CONST * uSkyParams.z;
    float zenith = acos(max(0.0, dir.y));
    float inv = 1.0 / (cos(zenith) + 0.15 * pow(93.885 - degrees(zenith), -1.253));
    vec3 fex = exp(-(betaR * 8.4E3 * inv + betaM * 1.25E3 * inv));
    float cosTheta = dot(dir, uSunDir);
    float half_ = cosTheta * 0.5 + 0.5;
    vec3 betaRTheta = betaR * (3.0 / (16.0 * FIO_PI)) * (1.0 + half_ * half_);
    float g = uSkyParams.w;
    vec3 betaMTheta = betaM * (1.0 / (4.0 * FIO_PI))
                    * ((1.0 - g * g) / pow(1.0 - 2.0 * g * cosTheta + g * g, 1.5));
    vec3 scatter = sunE * ((betaRTheta + betaMTheta) / (betaR + betaM));
    vec3 lin = pow(scatter * (1.0 - fex), vec3(1.5));
    lin *= mix(vec3(1.0), pow(scatter * fex, vec3(0.5)),
               clamp(pow(1.0 - uSunDir.y, 5.0), 0.0, 1.0));
    vec3 colour = (lin + vec3(0.1) * fex) * 0.04 + vec3(0.0, 0.0003, 0.00075);
    return pow(colour, vec3(1.0 / (1.2 + 1.2 * sunfade)));
}

void main() {
    vec3 dir;
    if (uMode == 2) {
        vec2 uv = vec2(vUV.x, uSourceParams.y > 0.5 ? vUV.y : 1.0 - vUV.y);
        FragColor = vec4(fioToLinear(textureLod(uSource, uv, 0.0).rgb) * uSourceParams.x, 1.0);
        return;
    }
    if (uMode == 1) {
        float phi = (vUV.x - 0.5) * 2.0 * FIO_PI;
        float elev = (0.5 - vUV.y) * FIO_PI;
        dir = vec3(cos(elev) * sin(phi), sin(elev), -cos(elev) * cos(phi));
    } else {
        vec4 v = uInvProjection * vec4(vNdc, 1.0, 1.0);
        dir = normalize(mat3(uInvView) * (v.xyz / v.w));
    }
    vec3 sky = fioAtmosphere(normalize(vec3(dir.x, max(dir.y, 0.0), dir.z))) * uSkyParams2.x;
    float cosTheta = dot(dir, uSunDir);
    float cover = 0.0;
    if (dir.y > 0.004 && uCloud.x > 0.0) {
        highp vec2 q = uCamPos.xz + dir.xz * (uCloud.z / (dir.y + 0.07));
        float dens = fioCloudDensity(q);
        if (dens > 0.002) {
            // How much cloud lies between this one and the sun.
            highp vec2 sunward = normalize(uSunDir.xz + vec2(1e-5)) * (0.22 / uCloud.w);
            float behind = fioCloudDensityLow(q + sunward) * 0.6
                         + fioCloudDensityLow(q + sunward * 2.3) * 0.4;
            float lit = exp(-1.9 * behind) * (1.0 - 0.5 * dens);
            float grey = dot(sky, vec3(0.2126, 0.7152, 0.0722));
            vec3 shade = mix(sky, vec3(grey), 0.55) * 0.62 + uSunColor * 0.035;
            vec3 bright = uSunColor * 0.34 + mix(sky, vec3(grey), 0.4) * 0.55;
            vec3 cloud = mix(shade, bright, lit) * uSkyParams2.z;
            // The thin edge of a cloud in front of the sun glows.
            cloud += uSunColor * pow(max(cosTheta, 0.0), 48.0) * (1.0 - dens) * 0.55;
            cover = dens * smoothstep(0.004, 0.16, dir.y);
            sky = mix(sky, cloud, cover);
        }
    }
    if (uMode == 0) {
        float disc = smoothstep(0.99972, 0.99990, cosTheta);
        sky += uSunColor * uSkyParams2.y * disc * (1.0 - cover) * step(0.0, dir.y);
    }
    sky = mix(sky, uGroundColor, smoothstep(0.0, -0.12, dir.y));
    FragColor = vec4(uMode == 0 ? fioEncode(sky) : sky, 1.0);
}
"""


class Environment:
    """What lights the world. Every field is plain data; set them and the
    next frame uses them. ``enabled`` False (the default) is Fio's stock
    renderer."""

    def __init__(self):
        self.enabled = False
        #: ``procedural`` (this module's sky), ``panorama`` (the map's sky
        #: image, drawn as classic Fio draws it and used as the environment
        #: map) or ``none`` (indoors: no sky light at all).
        self.sky_mode = hd_graphics.SKY_PROCEDURAL
        self.sun_enabled = True
        # -- the sun -----------------------------------------------------
        #: Compass bearing of the sun (degrees, 0 = +Z, 90 = +X) and its
        #: height above the horizon.
        self.sun_azimuth = 135.0
        self.sun_elevation = 42.0
        #: Linear RGB of sunlight on a surface facing it; ``None`` takes the
        #: colour the atmosphere gives the sun at this elevation.
        self.sun_color = None
        self.sun_intensity = 3.4
        self.shadow_size = 4096
        self.shadow_strength = 1.0
        #: How far around the view point the shadow map reaches when the
        #: world is larger than this (world units).
        self.shadow_range = 9000.0
        #: ``(x0, z0, x1, z1)``: the part of the world the shadow map covers
        #: (casters outside still shadow it); None covers every caster.
        self.shadow_focus = None
        # -- the sky -----------------------------------------------------
        self.turbidity = 3.5
        self.rayleigh = 1.4
        self.mie = 0.005
        self.mie_g = 0.8
        self.sky_intensity = 1.0
        self.sun_disc = 28.0
        #: Linear albedo of the ground, for the light it bounces back up.
        self.ground_color = (0.20, 0.19, 0.17)
        self.cloud_cover = 0.42
        self.cloud_softness = 0.22
        self.cloud_height = 2600.0
        self.cloud_size = 5200.0
        self.cloud_brightness = 1.0
        self.cloud_shadows = 0.55
        #: Cloud drift in world units per second, (x, z).
        self.wind = (26.0, 11.0)
        self.ambient_intensity = 1.0
        #: Aerial perspective: extinction per world unit, and how fast it
        #: thins with height.
        self.haze = 0.00003
        self.haze_falloff = 0.0008
        # -- surfaces ----------------------------------------------------
        self.roughness = 0.82
        self.metalness = 0.0
        #: Scale of parallax occlusion mapping: multiplies each material's
        #: own relief depth (its ``parallax``, world units). 0 turns it off.
        self.parallax = 1.0
        self.emissive_gain = 5.0
        self.point_light_gain = 3.0
        # -- the finished frame --------------------------------------------
        #: None: derived from the map's sky, sun, ambient and lamps
        #: (hd_graphics.base_exposure); a number overrides that.
        self.exposure = None
        #: Stops added to the exposure, whichever it is.
        self.exposure_compensation = 0.0
        self.bloom = 0.10
        self.bloom_threshold = 1.0
        self.ambient_occlusion = 1.0
        self.ao_radius = 56.0
        self.ao_taps = 12
        self.reflections = True
        self.ssr_steps = 40
        self.fxaa = True
        self.saturation = 1.04
        self.contrast = 1.05
        #: Linear RGB multiplier applied before tone mapping (white balance).
        self.tint = (1.0, 1.0, 1.0)
        self.vignette = 0.22
        self.grain = 0.012
        self.sun_shafts = 0.35

    def sun_direction(self):
        """Unit vector from the world toward the sun."""
        azimuth = math.radians(self.sun_azimuth)
        elevation = math.radians(max(1.0, min(89.5, self.sun_elevation)))
        c = math.cos(elevation)
        return (math.sin(azimuth) * c, math.sin(elevation), math.cos(azimuth) * c)

    def sun_radiance(self):
        """Linear RGB of sunlight on a surface facing the sun."""
        if not self.sun_enabled or self.sky_mode == hd_graphics.SKY_NONE:
            return (0.0, 0.0, 0.0)
        if self.sun_color is not None:
            colour = np.asarray(self.sun_color, dtype=np.float64)
        else:
            # The atmosphere's own extinction along the path to the sun
            # (the sky shader's fex), normalised to keep the brightness
            # sun_intensity asks for.
            zenith = math.radians(90.0 - max(1.0, min(89.5, self.sun_elevation)))
            inv = 1.0 / (math.cos(zenith)
                         + 0.15 * (93.885 - math.degrees(zenith)) ** -1.253)
            beta_r = np.array([5.804542996261093e-6, 1.3562911419845635e-5,
                               3.0265902468824876e-5]) * self.rayleigh
            beta_m = (0.434 * (0.2 * self.turbidity) * 10e-18 * np.array(
                [1.8399918514433978e14, 2.7798023919660528e14,
                 4.0790479543861094e14]) * self.mie)
            colour = np.exp(-(beta_r * 8.4e3 * inv + beta_m * 1.25e3 * inv))
            colour = colour / max(float(colour.max()), 1e-6)
            colour = 0.35 + 0.65 * colour          # the sun never reads as orange paint
        return tuple(float(v) * self.sun_intensity for v in colour)

    def key(self):
        """Every value, for telling when something changed."""
        return tuple(
            tuple(v) if isinstance(v, (list, tuple)) else v
            for _name, v in sorted(vars(self).items()))


class _Program:
    """A linked program and its uniform locations, looked up once."""

    def __init__(self, vertex, fragment):
        self.id = compileProgram(compileShader(vertex, gl.GL_VERTEX_SHADER),
                                 compileShader(fragment, gl.GL_FRAGMENT_SHADER),
                                 validate=False)
        self._locations = {}

    def __getitem__(self, name):
        location = self._locations.get(name)
        if location is None:
            location = self._locations[name] = gl.glGetUniformLocation(self.id, name)
        return location

    def delete(self):
        if self.id:
            gl_state.glDeleteProgram(self.id)
            self.id = 0


def data_image_source(path):
    """What :func:`decode_data_image` opens for *path*: the file itself, or
    an archived asset's bytes. Resolved on the render thread (the archives
    are not asked from another); the decode can then run anywhere."""
    if os.path.isabs(path) and os.path.isfile(path):
        return path                                 # a generated map in the cache
    from engine.animated_texture import _source
    return _source(path)


def decode_data_image(path, source, alpha_path=None, alpha_source=None):
    """The pixels of a normal or material map, bottom row first, as
    ``(width, height, RGBA bytes)``, or None. With *alpha_path*, that image's
    brightness becomes the alpha channel (a height map beside a normal map).
    Pure PIL work, no GL: run on a worker thread (_material_for)."""
    try:
        from PIL import Image
        from engine.animated_texture import check_texture_size
        with Image.open(source) as opened:
            check_texture_size(opened.width, opened.height, path)
            image = opened.convert('RGBA')
        if alpha_path:
            with Image.open(alpha_source) as opened:
                check_texture_size(opened.width, opened.height, alpha_path)
                height = opened.convert('L')
            if height.size != image.size:
                height = height.resize(image.size, Image.BILINEAR)
            image.putalpha(height)
        image = image.transpose(Image.FLIP_TOP_BOTTOM)
        return image.width, image.height, image.tobytes()
    except Exception as exc:
        print(f"[Environment] could not load {path}: {exc}")
        return None


def load_data_texture(path, anisotropy=0.0, alpha_path=None):
    """The image at *path* as a mip-mapped RGBA texture of plain data (a
    normal or material map: no colour space), or 0, decoded and uploaded
    here and now. (_material_for decodes on a worker thread instead and
    uploads with :func:`upload_data_texture`.)"""
    try:
        source = data_image_source(path)
        alpha_source = data_image_source(alpha_path) if alpha_path else None
    except Exception as exc:
        print(f"[Environment] could not load {path}: {exc}")
        return 0
    return upload_data_texture(decode_data_image(path, source, alpha_path, alpha_source),
                               anisotropy)


def upload_data_texture(decoded, anisotropy=0.0):
    """A :func:`decode_data_image` result as a mip-mapped RGBA8 texture, or
    0 for None. Leaves it bound on the active unit."""
    if not decoded:
        return 0
    width, height, pixels = decoded
    texture = int(gl.glGenTextures(1))
    gl_state.glBindTexture(gl.GL_TEXTURE_2D, texture)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                       gl.GL_LINEAR_MIPMAP_LINEAR)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
    gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, width, height,
                    0, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, pixels)
    gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
    gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
    _set_anisotropy(anisotropy)
    return texture


#: GL_TEXTURE_MAX_ANISOTROPY (EXT / 4.6 core share the value).
_TEXTURE_MAX_ANISOTROPY = 0x84FE
_MAX_TEXTURE_MAX_ANISOTROPY = 0x84FF


def _set_anisotropy(amount):
    """Anisotropic filtering for the bound 2D texture, where there is any."""
    if amount <= 1.0:
        return
    try:
        gl.glTexParameterf(gl.GL_TEXTURE_2D, _TEXTURE_MAX_ANISOTROPY, float(amount))
    except Exception:
        pass
    while gl.glGetError() != gl.GL_NO_ERROR:
        pass


class EnvironmentMixin:
    """HDHighRenderer's sun, sky and physically shaded surfaces."""

    def _init_environment(self):
        """State only: no GL object is made and no file is read here."""
        #: What is switched on: hd_graphics.HD_HIGH, or a copy of it with
        #: effects off that the benchmark sets to cost them.
        self.hd_preset = dict(hd_graphics.HD_HIGH)
        #: This is the HD renderer (the host asks: a classic renderer has
        #: no such attribute).
        self.hd = True
        #: False draws the editor view with the legacy shading this renderer
        #: still carries (the View menu's "HD preview"); Play always draws HD.
        self.hd_preview = True
        #: The map's HD sky (hd_graphics.sky_settings), set by the host.
        self._hd_sky = hd_graphics.sky_settings({}, "")
        self._hd_applied = None
        self._env_base_exposure = hd_graphics.INDOOR_DEFAULT_EXPOSURE
        self._env_exposure_key = None
        self._env_lamps_key = None
        self._env_lamp_light = 0.0
        #: (up, side, down, mean) of the loaded panorama, linear 0..1.
        self._env_panorama_light = None
        self._env_panorama_source = None
        self._env_reflect_map = 0
        self._env_reflect_key = None
        self._env_probe_reflections = False
        self._env_fog = None
        self._env_clear_colour = None
        self._env_drift = (0.0, 0.0)
        self._hdr_ubo = 0
        self._hdr_state = None
        self._hd_programs = []
        self._env_portal_phase = []
        self._env_shadow_static_tex = 0
        self._env_shadow_static_sig = None
        self._env_shadow_dynamic_sig = None
        # The sky light volume (skylight.py): the texture, the grid it was
        # built for, what it was built from, and when.
        self._env_skylight_tex = 0
        self._env_skylight_grid = None
        self._env_skylight_sig = None
        self._env_skylight_ready = False
        self._env_skylight_built = 0.0
        self._env_skylight_blank = 0
        #: What a reflection finds where no sky can be seen (linear).
        self._env_indoor_colour = (0.0, 0.0, 0.0)
        # Exposure follows where the view is: under the sky or under a roof.
        self._env_exposure_indoor = hd_graphics.classic_exposure()
        self._env_exposure_outdoor = hd_graphics.INDOOR_DEFAULT_EXPOSURE
        self._env_openness = None           # smoothed share of sky overhead
        self._env_openness_target = 1.0
        self._env_openness_time = None
        self._env_openness_frame = 0
        self._env_shadow_frame = None
        #: Sun shadow map work, for the benchmark: full (static) redraws,
        #: dynamic-layer redraws, casters drawn.
        self.sun_shadow_stats = {'static': 0, 'dynamic': 0, 'casters': 0}
        #: Set its fields (and ``enabled``) to light the world physically.
        self.environment = Environment()
        #: Whether this renderer's lit programs carry the physical path
        #: (false in low-power mode, which keeps its own shaders).
        self.surface_shading = False
        self._env_active = False
        self._env_phase = 0
        self._env_key = None
        self._env_started = time.perf_counter()
        self._env_time = 0.0
        self._env_frame = 0
        self._env_surface_state = {}
        self._env_material_bound = {}
        #: Programs whose uSurfaceDefault holds a model's own values.
        self._env_surface_default = {}
        self._env_sun_matrix = glm.mat4(1.0)
        self._env_sun_texel = 1.0
        self._env_shadow_tex = 0
        self._env_shadow_fbo = 0
        self._env_shadow_static_fbo = 0
        self._env_shadow_size = 0
        self._env_shadow_ready = False
        self._env_programs = {}
        self._env_vao = 0
        self._env_noise_tex = 0
        self._env_map = 0
        self._env_map_fbo = 0
        self._env_map_key = None
        self._env_map_age = 0
        self._env_ambient = ((0.3, 0.4, 0.6), (0.3, 0.3, 0.3), (0.1, 0.1, 0.1))
        self._env_ground = (0.1, 0.1, 0.1)
        self._env_sun_dir = (0.0, 1.0, 0.0)
        self._env_sun_rgb = (1.0, 1.0, 1.0)
        self._env_anisotropy = None
        # Forward+: the light grid (see _env_light_grid).
        self._env_grid_tex = 0
        self._env_grid_key = None
        self._env_grid_shape = None
        self._env_grid_params = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        self._env_grid_sent = {}
        #: For the benchmark: grids built, whether one is in use, and the
        #: lamps a cell lists (mean over the cells any lamp reaches, largest).
        self.light_grid_stats = {'builds': 0, 'on': False, 'lamps': 0,
                                 'mean_per_cell': 0.0, 'max_per_cell': 0}
        #: How many textures had their maps made at load (no files beside them).
        self.material_stats = {'generated': 0}
        #: GL diffuse texture -> (normal map, material map, parallax,
        #: emission) or None when the texture has no companion maps.
        self._material_maps = {}
        self._material_names = {}
        #: GL texture -> its maps being made or decoded (_material_collect).
        self._material_loading = {}
        self._material_uploads_left = self.MATERIAL_UPLOADS_PER_FRAME
        #: GL texture -> whether it has no alpha channel (_bind_material_maps).
        self._texture_opaque = {}
        self._texture_opaque_count = -1
        #: The maps on the material units now (units are shared by programs).
        self._env_units_material = None
        self._init_postfx()

    # ------------------------------------------------------------------
    # Programs and shared resources
    # ------------------------------------------------------------------
    def _install_surface_shading(self):
        """Have every program compiled from here on carry its HD path: the
        lit programs the physical one (surface.surface_source), the rest the
        display-to-scene conversion (hdr.classic_source). Called before the
        shaders compile, and only in an HD renderer."""
        if self.lowpower_mode or not self.hd:
            return
        plain = self.shader_loader.compile_from_source

        def compile_from_source(vertex_src, fragment_src):
            program = plain(vertex_src, self._hd_fragment_source(fragment_src))
            self._hd_program_ready(program)
            return program

        self.shader_loader.compile_from_source = compile_from_source
        self.surface_shading = True

    def _hd_fragment_source(self, source):
        """*source* as an HD renderer compiles it; *source* itself at low.
        For the programs that are compiled without the shader loader."""
        if not self.hd or not self.surface_shading:
            return source
        # Every point shadow map through one sampler (lighting.py): with a
        # cube sampler per slot the lit programs need more than the 16 a
        # fragment shader is guaranteed.
        source = point_shadow_source(source)
        if 'FioLightBlock' not in source:
            # Not yet through engine.shaders.light_ubo_source, which must
            # come after: it rewrites the light loops these paths add.
            physical = surface.surface_source(source)
            if physical is not source:
                return physical
            terrain = surface.terrain_source(source)
            if terrain is not None:
                return terrain
        if 'sampler2D behind' in source:
            return source       # redraws the frame's own pixels (portal fade)
        return hdr.classic_source(source)

    def _hd_program_ready(self, program):
        """Point a freshly linked program's ``FioHdr`` block at its buffer."""
        if not self.hd or not program:
            return
        index = gl.glGetUniformBlockIndex(int(program), "FioHdr")
        if index != gl.GL_INVALID_INDEX:
            gl.glUniformBlockBinding(int(program), index, hdr.HDR_BLOCK_BINDING)
            self._hd_programs.append(int(program))

    def _hdr_set(self, active):
        """Tell every classic program whether what it draws is an HD frame
        (one buffer update; nothing when it already knows)."""
        state = (bool(active), self._env_base_exposure)
        if state == self._hdr_state:
            return
        self._hdr_state = state
        exposure = max(float(self._env_base_exposure), 1e-6)
        data = np.array([1.0 if active else 0.0, 1.0 / exposure, exposure, 0.0],
                        dtype=np.float32)
        if not self._hdr_ubo:
            self._hdr_ubo = int(gl.glGenBuffers(1))
            gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self._hdr_ubo)
            gl.glBufferData(gl.GL_UNIFORM_BUFFER, data.nbytes, data, gl.GL_DYNAMIC_DRAW)
        else:
            gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, self._hdr_ubo)
            gl.glBufferSubData(gl.GL_UNIFORM_BUFFER, 0, data.nbytes, data)
        gl.glBindBuffer(gl.GL_UNIFORM_BUFFER, 0)
        gl.glBindBufferBase(gl.GL_UNIFORM_BUFFER, hdr.HDR_BLOCK_BINDING, self._hdr_ubo)

    # ------------------------------------------------------------------
    # The map's sky and the quality tier
    # ------------------------------------------------------------------
    def set_hd_sky(self, settings):
        """The map's HD sky: ``hd_graphics.sky_settings(...)``."""
        if settings is not None:
            self._hd_sky = settings

    def _hd_sync(self):
        """Bring the Environment up to date with the map's sky and the tier."""
        applied = (tuple(sorted(self.hd_preset.items())),
                   hd_graphics.settings_key(self._hd_sky))
        if applied != self._hd_applied:
            self._hd_applied = applied
            hd_graphics.apply(self.environment, self._hd_sky, self.hd_preset)

    def _configure_surface_programs(self):
        """Give each surface program's samplers their own texture units."""
        if not self.surface_shading:
            return
        for name in surface.SURFACE_PROGRAMS + ('terrain',):
            program = self.shaders.get(name)
            if not program:
                continue
            gl_state.glUseProgram(program)
            uniforms = self.uniforms[name]
            if uniforms['uSurfaceMode'] == -1:
                continue
            for sampler, unit in surface.SURFACE_SAMPLERS:
                location = uniforms[sampler]
                if location != -1:
                    gl_state.glUniform1i(location, unit)
            if name == 'terrain':
                for layer in range(4):
                    location = gl.glGetUniformLocation(program, "uLayerNormal[%d]" % layer)
                    if location != -1:
                        gl_state.glUniform1i(location, surface.TERRAIN_NORMAL_UNIT + layer)
        gl_state.glUseProgram(0)

    def _env_program(self, name, vertex, fragment):
        program = self._env_programs.get(name)
        if program is None:
            program = self._env_programs[name] = _Program(vertex, fragment)
        return program

    def _env_fullscreen(self):
        """Draw one triangle over the whole viewport."""
        if not self._env_vao:
            self._env_vao = int(gl.glGenVertexArrays(1))
        gl_state.glBindVertexArray(self._env_vao)
        gl.glDrawArrays(gl.GL_TRIANGLES, 0, 3)
        gl_state.glBindVertexArray(0)

    def _env_max_anisotropy(self):
        if self._env_anisotropy is None:
            try:
                value = float(gl.glGetFloatv(_MAX_TEXTURE_MAX_ANISOTROPY))
            except Exception:
                value = 0.0
            while gl.glGetError() != gl.GL_NO_ERROR:
                pass
            self._env_anisotropy = max(0.0, min(16.0, value))
        return self._env_anisotropy

    def _env_noise(self):
        """A 256x256 tileable texture of random values, for the clouds, the
        occlusion pass's rotations and the film grain."""
        if not self._env_noise_tex:
            rng = np.random.default_rng(20261123)
            data = rng.integers(0, 256, size=(256, 256, 4), dtype=np.uint8)
            self._env_noise_tex = int(gl.glGenTextures(1))
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_noise_tex)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_REPEAT)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, 256, 256, 0,
                            gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, data.tobytes())
        return self._env_noise_tex

    # ------------------------------------------------------------------
    # The frame
    # ------------------------------------------------------------------
    def _env_wanted(self, config, current_mode):
        """Whether this view is drawn with the environment."""
        if self._portal_scene_pass:
            # A portal's view is part of the frame it is drawn into.
            return self._env_active and not self._probe_capture_pass
        # The map's sky and the editor's HD preview come with the frame.
        sky = config.get('hd_sky', None)
        if sky is not None:
            self._hd_sky = sky
        preview = config.get('hd_preview', None)
        if preview is not None:
            self.hd_preview = bool(preview)
        if (not self.surface_shading or current_mode != RENDER_MODE_LIT
                or self._probe_capture_pass or self._shader_init_failed
                or not (self.hd_preview or config.get('play_mode', False))
                or config.get('brush_display_mode', 'Textured') != 'Textured'):
            return False
        self._hd_sync()
        return bool(self.environment.enabled)

    def _env_begin(self, config, camera_pos):
        """Start an environment frame: bring the sky map and the sun's
        shadow up to date, then send the frame to the HDR target. Returns
        whether the frame is an environment frame."""
        if self._portal_scene_pass:
            # Inside the frame already begun: the opaque phase again, without
            # reflection data (the reflection pass traces the main view).
            self._env_portal_phase.append(self._env_phase)
            self._env_phase = 3
            return True
        env = self.environment
        self._env_time = time.perf_counter() - self._env_started
        self._env_frame += 1
        self._material_uploads_left = self.MATERIAL_UPLOADS_PER_FRAME
        key = env.key()
        if key != self._env_key:
            self._env_key = key
            self._env_surface_state.clear()
            self._env_sun_dir = env.sun_direction()
            self._env_sun_rgb = env.sun_radiance()
            self._env_shadow_static_sig = None
        try:
            self._env_update_map(config, camera_pos)
            self._env_update_exposure(config, camera_pos)
            self._env_update_reflections(config, camera_pos)
            if not self._postfx_begin():
                return False
        except Exception as exc:
            # A driver that cannot do this keeps the stock renderer.
            print(f"[Environment] disabled: {exc}")
            env.enabled = False
            self._hd_applied = None
            self.hd_preview = False
            self._env_active = False
            self._hdr_set(False)
            return False
        self._env_active = True
        self._env_phase = 1
        self._env_units_material = None
        self._hdr_set(True)
        self._env_frame_fog()
        # The clear colour is a display colour too.
        clear = [float(v) for v in gl.glGetFloatv(gl.GL_COLOR_CLEAR_VALUE)]
        self._env_clear_colour = clear
        scene = hdr.display_to_scene(clear[:3], self._env_base_exposure)
        gl.glClearColor(*[max(c, 0.0) ** (1.0 / 2.2) for c in scene], clear[3])
        return True

    def _env_end_portal(self):
        """A portal's view inside an HD frame is finished: back to the phase
        the frame was in."""
        self._env_phase = self._env_portal_phase.pop() if self._env_portal_phase else 1

    def _env_frame_fog(self):
        """The map's distance fog for the surface programs. Where a map has
        fog switched on, fog is what the distance fades to and the sky's haze
        is left out: one or the other, never both."""
        vd = self.view_distance
        if getattr(vd, 'fog_enabled', False):
            colour = hdr.display_to_scene(tuple(vd.fog_color)[:3], self._env_base_exposure)
        else:
            colour = None
        if colour != self._env_fog:
            self._env_fog = colour
            self._env_surface_state.clear()

    def _env_lamps(self, config):
        """The light the map's lamps give an average surface, kept while the
        set of lamps (how many, how far each reaches) stands: a flickering or
        moving lamp does not pump the exposure."""
        table = config.get('entity_table')
        slots = getattr(table, 'light_slots', None)
        if table is None or slots is None or not len(slots):
            self._env_lamps_key = None
            self._env_lamp_light = 0.0
            return 0.0
        params = table.light_params[slots]
        key = (len(slots), float(np.round(params[:, 1].sum(), 1)))
        if key != self._env_lamps_key:
            self._env_lamps_key = key
            colours = np.power(np.clip(table.light_color[slots], 0.0, None), 2.2)
            self._env_lamp_light = hd_graphics.lamp_light(
                (float(p[0]), float(p[1]), c) for p, c in zip(params, colours))
        return self._env_lamp_light

    #: The view's sky openness is measured every this many frames and eased
    #: toward over about this many seconds.
    OPENNESS_EVERY = 4
    OPENNESS_SECONDS = 0.9

    def _env_update_exposure(self, config, camera_pos=None):
        """The frame's base exposure.

        Two exposures are worked out when the sky, the sun, the ambient or
        the set of lamps changes (never per frame, and nothing is read back
        from the GPU): one for under the open sky, from the sky's and the
        sun's light, and one for under a roof, held near classic brightness
        (hd_graphics.base_exposure). The frame's is between them by how much
        sky is over the view point, eased over a moment as an eye adjusts:
        a map's courtyard is exposed for daylight and its corridors for
        their lamps. A map with no sky only has the second."""
        env = self.environment
        lamps = self._env_lamps(config)
        ambient = tuple(float(v) for v in getattr(self.view_distance, 'ambient', (0, 0, 0)))
        terrain = config.get('terrain', None)
        terrain = bool(terrain is not None and getattr(terrain, 'enabled', False))
        key = (self._env_map_key, ambient, self._env_lamps_key, env.exposure, terrain)
        if key != self._env_exposure_key:
            self._env_exposure_key = key
            own = hd_graphics.scene_light(ambient=tuple(a + 0.1 for a in ambient)) + lamps
            self._env_exposure_indoor = hd_graphics.base_exposure(own, indoor=True)
            sun, elevation = self._env_sun_rgb, env.sun_elevation
            if terrain and not sum(sun):
                # The terrain's own stand-in sun (surface.py).
                sun = hd_graphics.TERRAIN_SUN_RADIANCE
                elevation = hd_graphics.TERRAIN_SUN_ELEVATION
            self._env_exposure_outdoor = hd_graphics.base_exposure(
                hd_graphics.scene_light(
                    sky_irradiance=self._env_ambient[0], sun_radiance=sun,
                    sun_elevation=elevation,
                    ambient=tuple(a + 0.1 for a in ambient)))
            # Where no sky can be seen and the frame has no answer, a
            # reflection finds the map's ambient and nothing more: a room's
            # lamps reach its surfaces directly, not by way of a made-up grey.
            level = hd_graphics.TYPICAL_ALBEDO * hd_graphics.scene_light(
                ambient=tuple(a + 0.1 for a in ambient))
            self._env_indoor_colour = (level, level, level)
        if env.exposure is not None:
            base = float(env.exposure)
        elif env.sky_mode == hd_graphics.SKY_NONE:
            base = self._env_exposure_indoor
        else:
            openness = self._env_view_openness(config, camera_pos)
            base = math.exp(math.log(self._env_exposure_indoor) * (1.0 - openness)
                            + math.log(self._env_exposure_outdoor) * openness)
        if os.environ.get('FIO_HD_DEBUG') and self._env_frame % 10 == 1:
            print("[HD] sky=%s exposure=%.3f indoor=%.3f outdoor=%.3f openness=%s"
                  % (env.sky_mode, base, self._env_exposure_indoor,
                     self._env_exposure_outdoor, self._env_openness))
        if abs(base - self._env_base_exposure) > 1e-4 * max(base, 1e-6):
            self._env_base_exposure = base
            self._env_surface_state.clear()

    #: Directions the view point looks for sky in: straight up and two rings.
    _OPENNESS_RAYS = np.array(
        [(0.0, 1.0, 0.0)]
        + [(math.cos(a) * 0.5, 0.866, math.sin(a) * 0.5)
           for a in np.linspace(0.0, 2.0 * math.pi, 6, endpoint=False)]
        + [(math.cos(a) * 0.819, 0.574, math.sin(a) * 0.819)
           for a in np.linspace(0.3, 2.0 * math.pi + 0.3, 8, endpoint=False)],
        dtype=np.float64)

    def _env_view_openness(self, config, camera_pos):
        """How much sky is over the view point, 0..1, eased over time.

        Rays from the view point toward the upper sky are tested against
        the boxes of the map's shadow-casting brushes (the render table's
        columns: no GL, no read-back); the share that reach the sky is the
        target, measured every few frames."""
        now = self._env_time
        if camera_pos is None:
            return 1.0 if self._env_openness is None else self._env_openness
        self._env_openness_frame += 1
        if self._env_openness is None or self._env_openness_frame >= self.OPENNESS_EVERY:
            self._env_openness_frame = 0
            share = self._env_sky_share(config, self._camera_xyz(camera_pos))
            if share is None:
                # Nothing to stand under yet (the map is still arriving).
                return 1.0 if self._env_openness is None else self._env_openness
            self._env_openness_target = share
        if self._env_openness is None or self._env_openness_time is None:
            self._env_openness = self._env_openness_target      # a new view: at once
        else:
            elapsed = max(0.0, min(now - self._env_openness_time, 0.25))
            blend = 1.0 - math.exp(-elapsed / self.OPENNESS_SECONDS)
            self._env_openness += (self._env_openness_target - self._env_openness) * blend
            if abs(self._env_openness_target - self._env_openness) < 0.004:
                self._env_openness = self._env_openness_target
        self._env_openness_time = now
        return self._env_openness

    def _env_sky_share(self, config, origin):
        """The share of :data:`_OPENNESS_RAYS` from *origin* that no
        shadow-casting brush's box stands in the way of; None while there
        are no brushes at all to tell by."""
        table = config.get('render_table')
        all_slots = config.get('all_brush_slots')
        if table is None or all_slots is None or not len(all_slots):
            return None
        casters = self._kept_shadow_casters(table, all_slots)
        if not len(casters):
            return 1.0
        centre = table.center[casters].astype(np.float64)
        half = table.half[casters].astype(np.float64)
        origin = np.asarray(origin, dtype=np.float64)
        # Boxes the view point is inside (a volume it walks through) do not count.
        outside = (np.abs(centre - origin) > half).any(axis=1)
        low = (centre - half)[outside] - origin
        high = (centre + half)[outside] - origin
        if not len(low):
            return 1.0
        rays = self._OPENNESS_RAYS
        with np.errstate(divide='ignore', invalid='ignore'):
            inverse = 1.0 / rays                                   # (R, 3)
            t0 = low[None, :, :] * inverse[:, None, :]             # (R, N, 3)
            t1 = high[None, :, :] * inverse[:, None, :]
        # A ray parallel to a slab is inside it or not, whatever t is.
        parallel = (rays == 0.0)[:, None, :]
        within = (low[None, :, :] <= 0.0) & (high[None, :, :] >= 0.0)
        near = np.where(parallel, np.where(within, -np.inf, np.inf), np.minimum(t0, t1))
        far = np.where(parallel, np.where(within, np.inf, -np.inf), np.maximum(t0, t1))
        enter = near.max(axis=2)
        leave = far.min(axis=2)
        blocked = ((leave >= np.maximum(enter, 0.0)) & (leave > 0.0)).any(axis=1)
        return float(1.0 - blocked.mean())

    def _env_reflect_fallback(self):
        """What a reflection finds under the horizon of a sky, where walls
        would be: a dimmer version of the sky's side light. Indoors the
        environment map is that colour already."""
        side = self._env_ambient[1]
        if self.environment.sky_mode == hd_graphics.SKY_NONE:
            return tuple(self._env_ground)
        return (side[0] * 0.45, side[1] * 0.45, side[2] * 0.45)

    def _env_reflection_gain(self):
        """What the reflection fallback's texels are multiplied by. A probe
        is a display picture of classic Fio's: a mid-grey of it reflects as
        a mid-grey of the frame, at whatever the exposure now is."""
        if not self._env_probe_reflections:
            return 1.0
        return (hd_graphics.KEY_VALUE / hd_graphics.MID_GREY
                / max(self._env_base_exposure, 1e-6))

    def _env_light(self, config, camera_pos):
        """Bring the sun's shadow map up to date and bind what every lit
        program reads this frame. After the frame's own bookkeeping, before
        the first lit pass."""
        if self._portal_scene_pass:
            return          # the frame's own are bound and current
        try:
            self._env_sun_shadows(config, camera_pos)
        except Exception as exc:
            print(f"[Environment] sun shadows off: {exc}")
            self._hd_sky = dict(self._hd_sky, shadow_strength=0.0)
            self.environment.shadow_strength = 0.0
            self._env_shadow_ready = False
        try:
            self._env_sky_light(config, camera_pos)
        except Exception as exc:
            print(f"[Environment] sky light off: {exc}")
            self._env_skylight_ready = False
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.SUN_SHADOW_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_shadow_tex)
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.SKY_LIGHT_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_3D, self._env_skylight_tex_or_blank())
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.ENV_MAP_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_reflection_texture())
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.NOISE_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_noise())
        gl_state.glActiveTexture(gl.GL_TEXTURE0)

    def _env_cloud_uniforms(self, program):
        env = self.environment
        size = max(float(env.cloud_size), 1.0)
        gl_state.glUniform4f(program['uCloud'], env.cloud_cover, max(env.cloud_softness, 0.01),
                       env.cloud_height, 1.0 / size)
        t = self._env_time
        gl_state.glUniform2f(program['uCloudOffset'],
                       (env.wind[0] * t / size) % 256.0, (env.wind[1] * t / size) % 256.0)

    # ------------------------------------------------------------------
    # Sky
    # ------------------------------------------------------------------
    def _env_sky_uniforms(self, program, mode, camera):
        env = self.environment
        gl_state.glUniform1i(program['uMode'], mode)
        gl_state.glUniform3f(program['uSunDir'], *self._env_sun_dir)
        gl_state.glUniform3f(program['uSunColor'], *self._env_sun_rgb)
        gl_state.glUniform4f(program['uSkyParams'], env.turbidity, env.rayleigh, env.mie, env.mie_g)
        gl_state.glUniform4f(program['uSkyParams2'], env.sky_intensity, env.sun_disc,
                       env.cloud_brightness, 0.0)
        gl_state.glUniform3f(program['uGroundColor'], *self._env_ground)
        gl_state.glUniform3f(program['uCamPos'], *camera)
        self._env_cloud_uniforms(program)
        gl_state.glUniform1i(program['uNoise'], surface.NOISE_UNIT)
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.NOISE_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_noise())
        gl_state.glActiveTexture(gl.GL_TEXTURE0)

    def _env_draws_sky(self):
        """Whether this HD frame's background is the procedural sky (any
        other is the panorama or the clear colour, drawn as always)."""
        return (self.environment.sky_mode == hd_graphics.SKY_PROCEDURAL
                and not self._portal_scene_pass)

    def _env_measure_panorama(self, image):
        """Sky light of the panorama just loaded (a PIL image)."""
        try:
            small = image.convert("RGB").resize((64, 32))
            self._env_panorama_light = hd_graphics.panorama_irradiance(np.asarray(small))
        except Exception as exc:
            print(f"[Environment] could not measure the panorama: {exc}")
            self._env_panorama_light = None
        self._env_map_key = None

    def _env_draw_sky(self, projection, view, behind=False):
        """The sky, behind everything drawn so far (*behind*) or over the
        whole frame."""
        program = self._env_program('sky', FULLSCREEN_VERT, SKY_FRAG)
        if behind:
            gl_state.glEnable(gl.GL_DEPTH_TEST)
            gl_state.glDepthFunc(gl.GL_LEQUAL)
        else:
            gl_state.glDisable(gl.GL_DEPTH_TEST)
        gl_state.glDepthMask(gl.GL_FALSE)
        gl_state.glDisable(gl.GL_BLEND)
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl_state.glUseProgram(program.id)
        # Held in locals: value_ptr points into the matrix it is given.
        inverse_projection = glm.inverse(projection)
        inverse_view = glm.inverse(view)
        uniform_matrix4(program['uInvProjection'], glm.value_ptr(inverse_projection))
        uniform_matrix4(program['uInvView'], glm.value_ptr(inverse_view))
        self._env_sky_uniforms(program, 0, self._frame_camera_pos)
        self._env_fullscreen()
        gl_state.glUseProgram(0)
        self._current_shader = None
        gl_state.glDepthMask(gl.GL_TRUE)
        gl_state.glEnable(gl.GL_DEPTH_TEST)
        gl_state.glDepthFunc(gl.GL_LESS)

    def _env_map_target(self):
        """The environment map (equirectangular, linear, mip-mapped) and
        the framebuffer the sky is drawn into it with."""
        if self._env_map:
            return
        width, height = ENV_MAP_SIZE
        self._env_map = self._env_equirect_texture(width, height)
        self._env_map_fbo = int(gl.glGenFramebuffers(1))

    @staticmethod
    def _env_equirect_texture(width, height):
        texture = int(gl.glGenTextures(1))
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA16F, width, height, 0,
                        gl.GL_RGBA, gl.GL_FLOAT, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_REPEAT)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                           gl.GL_LINEAR_MIPMAP_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        return texture

    def _env_drift_now(self):
        """How far the clouds have drifted, in cloud features."""
        env = self.environment
        size = max(float(env.cloud_size), 1.0)
        return (env.wind[0] * self._env_time / size, env.wind[1] * self._env_time / size)

    def _env_update_map(self, config, camera_pos):
        """Keep the environment map and the sky light taken from it.

        What the map holds depends on the sky: the procedural sky as this
        module draws it; the map's panorama; or, indoors, one dim colour
        from the map's ambient and lamps. It is redrawn when the sky's
        settings change and, for a procedural sky whose clouds are actually
        moving, once they have drifted far enough to show. The sky light is
        measured on the CPU (hd_graphics), never read back from the GPU."""
        env = self.environment
        mode = env.sky_mode
        if mode == hd_graphics.SKY_PANORAMA:
            panorama = self._background_panorama_texture()
            if not panorama or self._env_panorama_light is None:
                mode = hd_graphics.SKY_NONE         # no image to be a sky
            key = (mode, self._background_image_path, panorama, env.sky_intensity,
                   env.ambient_intensity)
        elif mode == hd_graphics.SKY_PROCEDURAL:
            key = (mode, env.sun_azimuth, env.sun_elevation, env.sun_color,
                   env.sun_intensity, env.sun_enabled,
                   env.turbidity, env.rayleigh, env.mie, env.mie_g, env.sky_intensity,
                   tuple(env.ground_color), env.cloud_cover, env.cloud_softness,
                   env.cloud_height, env.cloud_size, env.cloud_brightness,
                   env.ambient_intensity)
        if mode == hd_graphics.SKY_NONE:
            ambient = tuple(float(v) + 0.1
                            for v in getattr(self.view_distance, 'ambient', (0, 0, 0)))
            key = (mode, ambient, self._env_lamps_key)
        changed = key != self._env_map_key
        self._env_map_age += 1
        if not changed:
            if mode != hd_graphics.SKY_PROCEDURAL or env.cloud_cover <= 0.0:
                return
            if self._env_map_age < ENV_DRIFT_FRAMES:
                return
            drift = self._env_drift_now()
            if (abs(drift[0] - self._env_drift[0]) < ENV_DRIFT_DISTANCE
                    and abs(drift[1] - self._env_drift[1]) < ENV_DRIFT_DISTANCE):
                self._env_map_age = 0
                return
        self._env_map_target()
        width, height = ENV_MAP_SIZE
        if mode == hd_graphics.SKY_PROCEDURAL and changed:
            self._env_measure_procedural()
        elif mode == hd_graphics.SKY_PANORAMA:
            sky_gain = float(env.sky_intensity) * hd_graphics.PANORAMA_SKY_GAIN
            gain = sky_gain * float(env.ambient_intensity)
            up, side, down, _mean = self._env_panorama_light
            self._env_ambient = tuple(tuple(c * gain for c in colour)
                                      for colour in (up, side, down))
            self._env_ground = tuple(c * sky_gain for c in down)
        elif mode == hd_graphics.SKY_NONE:
            # No sky light at all; what a reflection finds where it finds
            # nothing else is a dim grey of the map's own light.
            level = hd_graphics.TYPICAL_ALBEDO * hd_graphics.luminance(
                hd_graphics.to_linear(ambient))
            self._env_ambient = ((0.0, 0.0, 0.0),) * 3
            self._env_ground = (level, level, level)
        camera = self._camera_xyz(camera_pos) if camera_pos is not None else (0.0, 0.0, 0.0)
        previous_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        previous_viewport = [int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT)]
        scissor = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        gl_state.glDisable(gl.GL_SCISSOR_TEST)
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._env_map_fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self._env_map, 0)
            gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])
            gl.glViewport(0, 0, width, height)
            gl_state.glDisable(gl.GL_DEPTH_TEST)
            gl_state.glDisable(gl.GL_BLEND)
            gl_state.glDepthMask(gl.GL_FALSE)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
            if mode == hd_graphics.SKY_NONE:
                previous_clear = [float(v) for v in gl.glGetFloatv(gl.GL_COLOR_CLEAR_VALUE)]
                gl.glClearColor(*self._env_ground, 1.0)
                gl.glClear(gl.GL_COLOR_BUFFER_BIT)
                gl.glClearColor(*previous_clear)
            else:
                program = self._env_program('sky', FULLSCREEN_VERT, SKY_FRAG)
                gl_state.glUseProgram(program.id)
                if mode == hd_graphics.SKY_PANORAMA:
                    self._env_image_uniforms(
                        program, panorama,
                        float(env.sky_intensity) * hd_graphics.PANORAMA_SKY_GAIN, flip=False)
                else:
                    self._env_sky_uniforms(program, 1, camera)
                self._env_fullscreen()
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_map)
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        finally:
            gl_state.glUseProgram(0)
            self._current_shader = None
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, 0)
            gl_state.glDepthMask(gl.GL_TRUE)
            gl_state.glEnable(gl.GL_DEPTH_TEST)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
            gl.glViewport(*previous_viewport)
            if scissor:
                gl_state.glEnable(gl.GL_SCISSOR_TEST)
        self._env_map_key = key
        self._env_map_age = 0
        self._env_drift = self._env_drift_now()
        self._env_surface_state.clear()

    def _env_image_uniforms(self, program, texture, gain, flip):
        """Sky program, mode 2: an equirectangular display image (the
        panorama, a reflection probe) into the environment map, linear."""
        gl_state.glUniform1i(program['uMode'], 2)
        gl_state.glUniform4f(program['uSourceParams'], gain, 1.0 if flip else 0.0, 0.0, 0.0)
        gl_state.glUniform1i(program['uSource'], 0)
        gl_state.glUniform1i(program['uNoise'], surface.NOISE_UNIT)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, int(texture))

    def _env_measure_procedural(self):
        """Sky light on surfaces facing up, sideways and down, and the
        ground's own colour under that sky, from the sky's formulas
        evaluated on the CPU (hd_graphics.procedural_sky_texels)."""
        env = self.environment
        arguments = dict(
            sun_direction=self._env_sun_dir, sun_radiance=self._env_sun_rgb,
            turbidity=env.turbidity, rayleigh=env.rayleigh, mie=env.mie, mie_g=env.mie_g,
            sky_intensity=env.sky_intensity, cloud_cover=env.cloud_cover,
            cloud_softness=env.cloud_softness, cloud_brightness=env.cloud_brightness)
        # First with a black ground, for the sky's light on it; then with the
        # ground lit by that sky and the sun.
        up = np.asarray(hd_graphics.irradiance_from_texels(
            hd_graphics.procedural_sky_texels(**arguments))[0])
        sun = np.asarray(self._env_sun_rgb) * max(self._env_sun_dir[1], 0.0)
        ground = np.asarray(env.ground_color, dtype=np.float64) * (up + sun * 0.55)
        self._env_ground = tuple(float(c) for c in ground)
        up, side, down, _mean = hd_graphics.irradiance_from_texels(
            hd_graphics.procedural_sky_texels(ground=self._env_ground, **arguments))
        gain = float(env.ambient_intensity)
        self._env_ambient = tuple(tuple(float(c) * gain for c in colour)
                                  for colour in (up, side, down))

    # ------------------------------------------------------------------
    # What a reflection finds where the frame has no answer
    # ------------------------------------------------------------------
    def _env_reflection_texture(self):
        """The equirectangular map reflections fall back on: the nearest
        reflection probe's snapshot where the map has probes, else the
        environment map (the sky, or indoors the map's dim ambient)."""
        return self._env_reflect_map if self._env_probe_reflections else self._env_map

    def _env_update_reflections(self, config, camera_pos):
        """Choose the reflection fallback for this view. With probes in the
        map, the nearest one's snapshot (a display image, as the water reads
        it) is redrawn into a linear, mip-mapped map of the frame's units
        whenever a different probe becomes the nearest."""
        frame = getattr(self, '_probe_frame', None)
        if not frame or not len(frame[0]):
            if self._env_probe_reflections:
                self._env_surface_state.clear()
            self._env_probe_reflections = False
            return
        positions, _radii, textures = frame
        if camera_pos is not None:
            camera = np.asarray(self._camera_xyz(camera_pos), dtype=np.float64)
            nearest = int(np.argmin(((np.asarray(positions, dtype=np.float64)
                                      - camera) ** 2).sum(axis=1)))
        else:
            nearest = 0
        texture = int(textures[nearest])
        if not texture:
            self._env_probe_reflections = False
            return
        key = texture
        if not self._env_probe_reflections:
            self._env_surface_state.clear()
        self._env_probe_reflections = True
        if key == self._env_reflect_key:
            return
        self._env_reflect_key = key
        width, height = ENV_MAP_SIZE[0] // 2, ENV_MAP_SIZE[1] // 2
        if not self._env_reflect_map:
            self._env_reflect_map = self._env_equirect_texture(width, height)
        self._env_map_target()
        previous_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        previous_viewport = [int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT)]
        scissor = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        gl_state.glDisable(gl.GL_SCISSOR_TEST)
        try:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._env_map_fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self._env_reflect_map, 0)
            gl.glDrawBuffers(1, [gl.GL_COLOR_ATTACHMENT0])
            gl.glViewport(0, 0, width, height)
            gl_state.glDisable(gl.GL_DEPTH_TEST)
            gl_state.glDisable(gl.GL_BLEND)
            gl_state.glDepthMask(gl.GL_FALSE)
            gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)
            gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
            program = self._env_program('sky', FULLSCREEN_VERT, SKY_FRAG)
            gl_state.glUseProgram(program.id)
            # Stored as the probe's own (linear) picture; brought into the
            # frame's units where it is read (_env_reflection_gain).
            self._env_image_uniforms(program, texture, 1.0, flip=False)
            self._env_fullscreen()
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_reflect_map)
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        finally:
            gl_state.glUseProgram(0)
            self._current_shader = None
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, 0)
            gl_state.glDepthMask(gl.GL_TRUE)
            gl_state.glEnable(gl.GL_DEPTH_TEST)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._env_map_fbo)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                      gl.GL_TEXTURE_2D, self._env_map, 0)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
            gl.glViewport(*previous_viewport)
            if scissor:
                gl_state.glEnable(gl.GL_SCISSOR_TEST)

    # ------------------------------------------------------------------
    # The sun's shadow map
    # ------------------------------------------------------------------
    def _env_shadow_target(self, size):
        """Two depth textures of *size*: the static casters' (kept until one
        of them changes) and the one the surfaces read (the static depth
        with what moves drawn over it)."""
        if self._env_shadow_tex and self._env_shadow_size == size:
            return
        for texture in (self._env_shadow_tex, self._env_shadow_static_tex):
            if texture:
                gl_state.glDeleteTextures([texture])
        textures = []
        for compare in (True, False):
            texture = int(gl.glGenTextures(1))
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_DEPTH_COMPONENT24, size, size, 0,
                            gl.GL_DEPTH_COMPONENT, gl.GL_FLOAT, None)
            filtering = gl.GL_LINEAR if compare else gl.GL_NEAREST
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, filtering)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, filtering)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_BORDER)
            gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_BORDER)
            gl.glTexParameterfv(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_BORDER_COLOR,
                                (1.0, 1.0, 1.0, 1.0))
            if compare:
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_COMPARE_MODE,
                                   gl.GL_COMPARE_REF_TO_TEXTURE)
                gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_COMPARE_FUNC, gl.GL_LEQUAL)
            textures.append(texture)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, 0)
        self._env_shadow_tex, self._env_shadow_static_tex = textures
        if not self._env_shadow_fbo:
            self._env_shadow_fbo = int(gl.glGenFramebuffers(1))
            self._env_shadow_static_fbo = int(gl.glGenFramebuffers(1))
        for framebuffer, texture in ((self._env_shadow_fbo, self._env_shadow_tex),
                                     (self._env_shadow_static_fbo,
                                      self._env_shadow_static_tex)):
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
            gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_ATTACHMENT,
                                      gl.GL_TEXTURE_2D, texture, 0)
            gl.glDrawBuffer(gl.GL_NONE)
            gl.glReadBuffer(gl.GL_NONE)
        self._env_shadow_size = size
        self._env_shadow_static_sig = None

    #: In a world larger than ``shadow_range`` the shadow map follows the
    #: view in steps of this fraction of its width, so it is redrawn once
    #: per step walked, not once per frame.
    SHADOW_FOLLOW_STEP = 1.0 / 8.0

    def _env_sun_frame(self, low, high, depth_low, depth_high, camera_pos):
        """The sun's view of the box *low*..*high* (world): ``(matrix, world
        units a texel, key)``.

        An orthographic box around the casters, or around the view point
        when the world is larger than ``shadow_range``; there it moves in
        coarse steps that are whole texels, so the shadows neither swim nor
        need redrawing every frame. *depth_low*..*depth_high* is every
        caster's box: it only adds depth."""
        env = self.environment
        direction = glm.vec3(*self._env_sun_dir)
        up = glm.vec3(0.0, 1.0, 0.0) if abs(direction.y) < 0.99 else glm.vec3(0.0, 0.0, 1.0)
        low, high = low.copy(), high.copy()
        focus = env.shadow_focus
        if focus:
            # What receives shadows: the focus, from the ground to a wall or
            # so above it. Taller and farther casters only add depth.
            low[0], low[2] = max(low[0], focus[0]), max(low[2], focus[1])
            high[0], high[2] = min(high[0], focus[2]), min(high[2], focus[3])
            high[1] = min(high[1], low[1] + 520.0)
        reach = float(env.shadow_range)
        size = int(self._env_shadow_size)
        follow = (max(high[0] - low[0], high[2] - low[2]) > 2.0 * reach
                  and camera_pos is not None)
        if follow:
            step = 2.0 * reach * self.SHADOW_FOLLOW_STEP
            cx, _cy, cz = self._camera_xyz(camera_pos)
            cx = math.floor(cx / step + 0.5) * step
            cz = math.floor(cz / step + 0.5) * step
            # Room for the view to walk half a step either way.
            low[0], high[0] = cx - reach - step, cx + reach + step
            low[2], high[2] = cz - reach - step, cz + reach + step
        middle = glm.vec3(*((low + high) * 0.5))
        look = glm.lookAt(middle + direction * 1000.0, middle, up)
        corners = np.array([[x, y, z, 1.0] for x in (low[0], high[0])
                            for y in (low[1], high[1]) for z in (low[2], high[2])])
        matrix = np.array(look.to_list(), dtype=np.float64).T      # row-major
        in_light = corners @ matrix.T
        lo = in_light[:, :3].min(axis=0)
        hi = in_light[:, :3].max(axis=0)
        # Depth takes in every caster, wherever it stands.
        everything = np.array([[x, y, z, 1.0] for x in (depth_low[0], depth_high[0])
                               for y in (depth_low[1], depth_high[1])
                               for z in (depth_low[2], depth_high[2])]) @ matrix.T
        lo[2] = min(lo[2], everything[:, 2].min())
        hi[2] = max(hi[2], everything[:, 2].max())
        span = max(hi[0] - lo[0], hi[1] - lo[1]) * 1.02
        texel = span / size
        mid_x = (lo[0] + hi[0]) * 0.5
        mid_y = (lo[1] + hi[1]) * 0.5
        if follow:
            mid_x = math.floor(mid_x / texel) * texel
            mid_y = math.floor(mid_y / texel) * texel
        projection = glm.ortho(mid_x - span * 0.5, mid_x + span * 0.5,
                               mid_y - span * 0.5, mid_y + span * 0.5,
                               -hi[2] - 16.0, -lo[2] + 16.0)
        key = (round(mid_x, 2), round(mid_y, 2), round(span, 2),
               round(float(lo[2]), 1), round(float(hi[2]), 1))
        return projection * look, texel, key, (low, high) if follow else None

    def _env_split_casters(self, table, casters):
        """*casters* as ``(static, moving)``: the movers and doors (the
        table's ``dynamic_slots``) apart from everything that stands still.
        Kept while both inputs are the arrays they were, so the reach memos
        behind them know their candidates by identity."""
        dynamic = getattr(table, 'dynamic_slots', None)
        held = self.__dict__.get('_env_caster_split')
        if (held is not None and held[0]() is table and held[1] is casters
                and held[2] is dynamic):
            return held[3], held[4]
        if dynamic is None or not len(dynamic):
            static, moving = casters, np.empty(0, dtype=np.int32)
        else:
            is_moving = np.isin(casters, dynamic)
            static = np.array(casters[~is_moving], dtype=np.int32)
            moving = np.array(casters[is_moving], dtype=np.int32)
        if held is not None and held[0]() is table:
            # The same rows as last time: hand back the same arrays.
            if np.array_equal(held[3], static):
                static = held[3]
            if np.array_equal(held[4], moving):
                moving = held[4]
        for array in (static, moving):
            if array is not casters:
                array.flags.writeable = False
        import weakref
        self.__dict__['_env_caster_split'] = (weakref.ref(table), casters, dynamic,
                                              static, moving)
        return static, moving

    def _env_casters(self, config):
        """The frame's shadow-casting brushes: ``(table, static, moving,
        low, high)`` -- those that stand still, the movers and doors, and
        the box (world, whole units) of what the maps cover -- or None."""
        table = config.get('render_table')
        all_slots = config.get('all_brush_slots')
        if table is None or all_slots is None:
            return None
        casters = self._kept_shadow_casters(table, all_slots)
        if not len(casters):
            return None
        static, moving = self._env_split_casters(table, casters)
        # The box the map covers: the casters that stand still (a door
        # opening must not move it), or all of them when everything moves.
        framing = static if len(static) else casters
        centre = table.center[framing]
        half = table.half[framing]
        low = (centre - half).min(axis=0).astype(np.float64)
        high = (centre + half).max(axis=0).astype(np.float64)
        # Whole units, so a caster's float jitter does not redraw a map.
        return table, static, moving, np.floor(low), np.ceil(high)

    def _env_skylight_tex_or_blank(self):
        """The sky light volume, or a one-cell open one while there is none
        (a sampler must have a complete texture of its own kind)."""
        if self._env_skylight_tex:
            return self._env_skylight_tex
        if not self._env_skylight_blank:
            self._env_skylight_blank = int(gl.glGenTextures(1))
            gl_state.glBindTexture(gl.GL_TEXTURE_3D, self._env_skylight_blank)
            for name in (gl.GL_TEXTURE_MIN_FILTER, gl.GL_TEXTURE_MAG_FILTER):
                gl.glTexParameteri(gl.GL_TEXTURE_3D, name, gl.GL_NEAREST)
            gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_RG16, 1, 1, 1, 0, gl.GL_RG,
                            gl.GL_UNSIGNED_SHORT, np.full((1, 1, 1, 2), 65535, dtype=np.uint16))
            gl_state.glBindTexture(gl.GL_TEXTURE_3D, 0)
        return self._env_skylight_blank

    #: While the map is being edited the volume is rebuilt at most this
    #: often (seconds): a brush being dragged changes the casters every
    #: frame, and a rebuild is tens of milliseconds of NumPy.
    SKY_LIGHT_INTERVAL = 0.25

    def _env_sky_light(self, config, camera_pos):
        """Keep the sky light volume (skylight.py): how much daylight
        reaches each part of the map -- all of it under the open sky, what
        spreads in through the openings indoors. The surface programs read
        it (surface.fioSkyVisible). Rebuilt only when one of the map's
        standing shadow casters changes; a map with no sky has none."""
        env = self.environment
        self._env_skylight_ready = False
        if env.sky_mode == hd_graphics.SKY_NONE:
            return
        found = self._env_casters(config)
        if found is None:
            return
        table, static, _moving, low, high = found
        if not len(static):
            return
        camera = self._camera_xyz(camera_pos) if camera_pos is not None else None
        grid = skylight.grid_for(low, high, camera)
        if grid.follows:
            extent = grid.extent
            where = (grid.origin[0] + extent[0] * 0.5, grid.origin[1] + extent[1] * 0.5,
                     grid.origin[2] + extent[2] * 0.5,
                     math.sqrt(extent[0] ** 2 + extent[1] ** 2 + extent[2] ** 2) * 0.5 + 1024.0)
        else:
            where = (0.0, 0.0, 0.0, 1.0e9)
        slots, keys = self._kept_casters_in_reach('sky light', table, static, *where)
        if not len(slots):
            return
        previous = self._env_skylight_sig
        current = (previous is not None and previous[0] == grid.key
                   and (previous[1] is keys or previous[1] == keys))
        if not current:
            now = time.perf_counter()
            if (self._env_skylight_tex and self._env_skylight_grid is not None
                    and now - self._env_skylight_built < self.SKY_LIGHT_INTERVAL):
                # Just rebuilt: the one there serves until the interval is up.
                self._env_skylight_ready = True
                return
            light, solid = skylight.build(table, slots, grid)
            texels = skylight.texels(light, solid)
            if not self._env_skylight_tex:
                self._env_skylight_tex = int(gl.glGenTextures(1))
                gl_state.glBindTexture(gl.GL_TEXTURE_3D, self._env_skylight_tex)
                for name, value in ((gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR),
                                    (gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR),
                                    (gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE),
                                    (gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE),
                                    (gl.GL_TEXTURE_WRAP_R, gl.GL_CLAMP_TO_EDGE)):
                    gl.glTexParameteri(gl.GL_TEXTURE_3D, name, value)
            else:
                gl_state.glBindTexture(gl.GL_TEXTURE_3D, self._env_skylight_tex)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
            nx, ny, nz = grid.shape
            gl.glTexImage3D(gl.GL_TEXTURE_3D, 0, gl.GL_RG16, nx, ny, nz, 0,
                            gl.GL_RG, gl.GL_UNSIGNED_SHORT, texels)
            gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
            gl_state.glBindTexture(gl.GL_TEXTURE_3D, 0)
            self._env_skylight_grid = grid
            self._env_skylight_sig = (grid.key, keys)
            self._env_skylight_built = now
            self.sun_shadow_stats['sky light'] = self.sun_shadow_stats.get('sky light', 0) + 1
        self._env_skylight_ready = True

    def _env_sun_shadows(self, config, camera_pos):
        """Bring the sun's shadow map up to date.

        The casters are drawn in two layers. Everything that stands still
        goes into a depth map of its own, redrawn only when one of those
        casters, the sun or the map's size changes. The movers, doors and
        models go over a copy of it, so a door swinging costs one depth copy
        and the door, not the whole world again."""
        env = self.environment
        self._env_shadow_ready = False
        if env.shadow_strength <= 0.0 or not self.shadows_enabled or not env.sun_enabled:
            return
        entity_table = config.get('entity_table')
        found = self._env_casters(config)
        if found is None or entity_table is None:
            return
        table, static, moving, low, high = found
        size = int(max(512, min(8192, env.shadow_size)))
        self._env_shadow_target(size)
        matrix, texel, frame_key, box = self._env_sun_frame(
            low, high, low - 64.0, high + 64.0, camera_pos)
        if box is not None:
            # A streamed world: only the casters that can reach the box.
            middle = (box[0] + box[1]) * 0.5
            reach = float(np.linalg.norm((box[1] - box[0])[[0, 2]])) * 0.5 + 1024.0
            where = (float(middle[0]), float(middle[1]), float(middle[2]), reach)
        else:
            where = (0.0, 0.0, 0.0, 1.0e9)
        static_slots, static_keys = self._kept_casters_in_reach(
            'sun', table, static, *where)
        moving_slots, moving_keys = self._kept_casters_in_reach(
            'sun moving', table, moving, *where)
        model_slots = self._dense_shadow_model_slots(
            entity_table, config.get('thing_hidden'))
        in_models, model_keys = self._collect_dense_shadow_models(
            entity_table, model_slots, where[0], where[1], where[2], where[3])
        if not len(static_slots) and not len(moving_slots) and not len(in_models):
            return
        self._env_sun_matrix = matrix
        self._env_sun_texel = texel
        self._env_shadow_ready = True
        frame = (frame_key, self._env_sun_dir, size)
        previous = self._env_shadow_static_sig
        static_current = (previous is not None and previous[0] == frame
                          and (previous[1] is static_keys or previous[1] == static_keys))
        previous = self._env_shadow_dynamic_sig
        dynamic_current = (static_current and previous is not None
                           and (previous[0] is moving_keys or previous[0] == moving_keys)
                           and previous[1] == model_keys)
        if static_current and dynamic_current:
            return
        self._env_shadow_static_sig = (frame, static_keys)
        self._env_shadow_dynamic_sig = (moving_keys, model_keys)
        self._env_draw_shadow_map(table, static_slots, moving_slots, entity_table,
                                  in_models, matrix, size, not static_current)

    def _env_depth_programs(self):
        """The depth-only programs: per-object and instanced, built from the
        cube-map depth pass's own vertex shaders."""
        programs = self._env_programs
        if 'sun_depth' not in programs:
            vertex = self._shader_source('depth_cube.vert')
            programs['sun_depth'] = _Program(vertex, SUN_DEPTH_FRAG)
            instanced = self._instanced_vertex_source(vertex, preamble='')
            programs['sun_depth_instanced'] = (
                _Program(instanced, SUN_DEPTH_FRAG)
                if instanced and self._cube_vbo is not None else None)
        return programs['sun_depth'], programs['sun_depth_instanced']

    def _env_draw_shadow_map(self, table, static, moving, entity_table, in_models,
                             matrix, size, redraw_static):
        plain, instanced = self._env_depth_programs()
        previous_fbo = int(gl.glGetIntegerv(gl.GL_FRAMEBUFFER_BINDING))
        previous_viewport = [int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT)]
        scissor = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        cull = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        matrix_ptr = glm.value_ptr(matrix)
        stats = self.sun_shadow_stats
        try:
            gl.glViewport(0, 0, size, size)
            gl_state.glDisable(gl.GL_SCISSOR_TEST)
            gl_state.glDisable(gl.GL_BLEND)
            gl_state.glEnable(gl.GL_DEPTH_TEST)
            gl_state.glDepthMask(gl.GL_TRUE)
            gl_state.glDepthFunc(gl.GL_LESS)
            gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
            gl_state.glEnable(gl.GL_POLYGON_OFFSET_FILL)
            if redraw_static:
                gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._env_shadow_static_fbo)
                gl.glClear(gl.GL_DEPTH_BUFFER_BIT)
                self._env_draw_brush_casters(table, static, plain, instanced, matrix_ptr,
                                             ('shadow', 'sun'))
                stats['static'] += 1
                stats['casters'] += len(static)
            # The static depth, then what moves over it.
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, self._env_shadow_static_fbo)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, self._env_shadow_fbo)
            gl.glBlitFramebuffer(0, 0, size, size, 0, 0, size, size,
                                 gl.GL_DEPTH_BUFFER_BIT, gl.GL_NEAREST)
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._env_shadow_fbo)
            self._env_draw_brush_casters(table, moving, plain, instanced, matrix_ptr,
                                         ('shadow', 'sun moving'))
            stats['dynamic'] += 1
            stats['casters'] += len(moving) + len(in_models)
            # Models need not be closed: both sides, pushed back a little more.
            gl_state.glUseProgram(plain.id)
            uniform_matrix4(plain['lightSpaceMatrix'], matrix_ptr)
            model_loc = plain['model']
            gl_state.glDisable(gl.GL_CULL_FACE)
            gl.glPolygonOffset(2.5, 4.0)
            recipes = entity_table.model_recipes()
            resolved = []
            for slot in in_models:
                slot = int(slot)
                recipe = int(entity_table.model_recipe_id[slot])
                if 0 <= recipe < len(recipes):
                    model = self.load_model(recipes[recipe][0])
                    if model and model.is_loaded:
                        resolved.append((slot, model))
            if resolved:
                slots = np.array([s for s, _ in resolved], dtype=np.intp)
                bases = entity_table.model_base_matrix[slots]
                bases[:, 12:15] = entity_table.pos[slots]
                upload = uniform_rows('mat4', bases)
                for index, (_slot, model) in enumerate(resolved):
                    upload(model_loc, index)
                    gl_state.glBindVertexArray(model.vao)
                    if getattr(model, 'ebo', None) is not None and getattr(model, 'index_count', 0):
                        gl.glDrawElements(gl.GL_TRIANGLES, model.index_count,
                                          gl.GL_UNSIGNED_INT, None)
                    else:
                        gl.glDrawArrays(gl.GL_TRIANGLES, 0, model.vertex_count)
        finally:
            gl_state.glBindVertexArray(0)
            gl_state.glDisable(gl.GL_POLYGON_OFFSET_FILL)
            gl_state.glCullFace(gl.GL_BACK)
            if cull:
                gl_state.glEnable(gl.GL_CULL_FACE)
            else:
                gl_state.glDisable(gl.GL_CULL_FACE)
            gl_state.glUseProgram(0)
            self._current_shader = None
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, previous_fbo)
            gl.glViewport(*previous_viewport)
            if scissor:
                gl_state.glEnable(gl.GL_SCISSOR_TEST)

    def _env_draw_brush_casters(self, table, in_brushes, plain, instanced, matrix_ptr, key):
        """Brush casters into the bound depth map."""
        if not len(in_brushes):
            return
        # Brushes are closed solids: drawing the faces turned away from
        # the sun puts the stored depth behind every lit face, which then
        # cannot shadow itself. Cube brushes are wound clockwise-outward,
        # convex meshes counter-clockwise (see PortalsMixin).
        gl_state.glEnable(gl.GL_CULL_FACE)
        gl_state.glCullFace(gl.GL_BACK)
        gl.glPolygonOffset(1.0, 1.0)
        cube_slots, geo_slots, stream = self._prepare_shadow_instances(
            table, in_brushes, instanced is not None, key=key)
        cube_vao = self.vaos['cube']
        if len(cube_slots) and stream is not None:
            gl_state.glUseProgram(instanced.id)
            uniform_matrix4(instanced['lightSpaceMatrix'], matrix_ptr)
            gl_state.glBindVertexArray(stream.vao)                 # reads from row 0
            gl.glDrawArraysInstanced(gl.GL_TRIANGLES, 0, 36, len(cube_slots))
        gl_state.glUseProgram(plain.id)
        uniform_matrix4(plain['lightSpaceMatrix'], matrix_ptr)
        model_loc = plain['model']
        if len(cube_slots) and stream is None:
            cube_models, _ = self._frame_transforms(table, cube_slots)
            upload = uniform_rows('mat4', cube_models)
            gl_state.glBindVertexArray(cube_vao)
            for index in range(len(cube_slots)):
                upload(model_loc, index)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, 36)
        if len(geo_slots):
            gl_state.glCullFace(gl.GL_FRONT)
            meshes = self._prepare_geo_meshes(table, geo_slots)
            geo_models, _ = self._frame_transforms(table, geo_slots)
            upload = uniform_rows('mat4', geo_models)
            for index, slot in enumerate(geo_slots):
                mesh = meshes.get(int(table.geometry_id[int(slot)]))
                if mesh is None:
                    continue
                upload(model_loc, index)
                gl_state.glBindVertexArray(mesh.vao)
                gl.glDrawArrays(gl.GL_TRIANGLES, 0, mesh.count)

    # ------------------------------------------------------------------
    # Surface programs
    # ------------------------------------------------------------------
    def _bind_surface(self, shader_name):
        """Give the bound lit program this frame's surface state. Values are
        program state, so each is sent only when it differs from what the
        program last had."""
        if not self.surface_shading:
            return
        uniforms = self.uniforms.get(shader_name)
        if uniforms is None:
            return
        mode_loc = uniforms['uSurfaceMode']
        if mode_loc == -1:
            return
        state = self._env_surface_state.get(shader_name)
        if not self._env_active:
            if state is not None and state[0] != 0:
                gl_state.glUniform1i(mode_loc, 0)
                self._env_surface_state[shader_name] = (0, None, None)
            return
        env = self.environment
        mode = self._env_phase
        if state is None or state[0] != mode:
            gl_state.glUniform1i(mode_loc, mode)
        frame = (self._env_map_key, self._env_shadow_ready, self._env_reflect_key,
                 self._env_probe_reflections, self._env_base_exposure,
                 self._env_skylight_ready, self._env_skylight_sig,
                 self._env_indoor_colour, self._env_reflect_fallback())
        if state is None or state[1] != frame:
            ambient = self._env_ambient
            gl_state.glUniform3f(uniforms['uSunDir'], *self._env_sun_dir)
            gl_state.glUniform3f(uniforms['uSunColor'], *self._env_sun_rgb)
            gl_state.glUniform3f(uniforms['uAmbientSky'], *ambient[0])
            gl_state.glUniform3f(uniforms['uAmbientHorizon'], *ambient[1])
            gl_state.glUniform3f(uniforms['uAmbientGround'], *ambient[2])
            probe = self._env_probe_reflections
            top_mip = math.log2(ENV_MAP_SIZE[0]) - (3.0 if probe else 2.0)
            gl_state.glUniform4f(uniforms['uEnvParams'], top_mip,
                           0.0 if self._postfx_reflects() else 1.0,
                           env.emissive_gain, env.point_light_gain)
            # With a probe on the environment unit, the haze cannot take its
            # colour from there: it gets the sky's horizon light instead.
            gl_state.glUniform4f(uniforms['uEnvParams2'], 1.0 if probe else 0.0,
                           1.0 if probe else 0.0, self._env_reflection_gain(), 0.0)
            grid = self._env_skylight_grid if self._env_skylight_ready else None
            if grid is None:
                gl_state.glUniform4f(uniforms['uSkyLightScale'], 1.0, 1.0, 1.0, 0.0)
            else:
                extent = grid.extent
                # A point is first moved this far off its surface, into the
                # room it faces.
                gl_state.glUniform4f(uniforms['uSkyLightOrigin'], grid.origin[0], grid.origin[1],
                               grid.origin[2], grid.cell * skylight.SURFACE_OFFSET)
                gl_state.glUniform4f(uniforms['uSkyLightScale'], 1.0 / extent[0], 1.0 / extent[1],
                               1.0 / extent[2], 2.0 if grid.follows else 1.0)
            gl_state.glUniform3f(uniforms['uHazeColor'], *self._env_ambient[1])
            gl_state.glUniform3f(uniforms['uReflectIndoor'], *self._env_indoor_colour)
            gl_state.glUniform3f(uniforms['uReflectFallback'], *self._env_reflect_fallback())
            fog = self._env_fog
            if fog is None:
                gl_state.glUniform4f(uniforms['uHdrFog'], 0.0, 0.0, 0.0, 0.0)
            else:
                gl_state.glUniform4f(uniforms['uHdrFog'], fog[0], fog[1], fog[2], 1.0)
            gl_state.glUniform4f(uniforms['uSurfaceDefault'], env.roughness, env.metalness, 0.0, 0.0)
            self._env_surface_default.pop(shader_name, None)
            gl_state.glUniform4f(uniforms['uAtmosphere'],
                           0.0 if self._env_fog is not None else env.haze,
                           env.haze_falloff,
                           env.cloud_shadows if env.cloud_cover > 0.0 else 0.0, 0.0)
            gl_state.glUniform4f(uniforms['uMaterial'], 0.0, 0.0, 0.0, 0.0)
            self._env_material_bound[shader_name] = None
        # The shadow matrix follows the view in a large world, and the
        # clouds drift: both every frame.
        size = float(max(self._env_shadow_size, 1))
        shadow = (glm.value_ptr(self._env_sun_matrix), self._env_sun_texel,
                  self._env_shadow_ready)
        if state is None or state[2] != self._env_frame:
            gl.glUniformMatrix4fv(uniforms['uSunMatrix'], 1, gl.GL_FALSE, shadow[0])
            gl_state.glUniform4f(uniforms['uSunShadowParams'], 1.0 / size, size,
                           self._env_sun_texel * 1.6,
                           env.shadow_strength if self._env_shadow_ready else 0.0)
            cloud_size = max(float(env.cloud_size), 1.0)
            t = self._env_time
            gl_state.glUniform4f(uniforms['uCloud'], env.cloud_cover,
                           max(env.cloud_softness, 0.01), env.cloud_height, 1.0 / cloud_size)
            gl_state.glUniform2f(uniforms['uCloudOffset'],
                           (env.wind[0] * t / cloud_size) % 256.0,
                           (env.wind[1] * t / cloud_size) % 256.0)
        self._env_surface_state[shader_name] = (mode, frame, self._env_frame)
        self._env_light_grid(shader_name, uniforms)
        if shader_name in self._env_surface_default:
            # A model's own roughness and metalness, from its last draw.
            gl_state.glUniform4f(uniforms['uSurfaceDefault'], env.roughness, env.metalness, 0.0, 0.0)
            del self._env_surface_default[shader_name]
        # A program that draws without materials of its own (models, glow
        # brushes) must not keep the last brush's maps.
        if self._env_material_bound.get(shader_name) is not None:
            gl_state.glUniform4f(uniforms['uMaterial'], 0.0, 0.0, 0.0, 0.0)
            self._env_material_bound[shader_name] = None

    #: Forward+ pays when there are enough lamps that most of them cannot
    #: reach a given fragment; under this many a fragment simply tests each.
    #: Measured (docs/hd_high_audit.md, "Forward+ threshold"): from 10 lamps
    #: the grid saved 12-35% of the scene pass however the lamps were laid
    #: out; under 6 it saved nothing where lamps overlap, and on real maps of
    #: 7-8 lamps 0-4%, too little to cover the cost of the per-fragment lamp
    #: index on a GPU (a divergent index into the light block, which a
    #: software rasteriser does not charge for).
    FORWARD_PLUS_MIN_LIGHTS = 10

    def _env_light_grid(self, shader_name, uniforms):
        """Forward+ for the bound surface program: bring the light grid up
        to date with the lamps now in the light block and tell the program
        whether to use it.

        The lamps (their place and reach, as just uploaded to the block)
        are binned on the CPU into clusters -- boxes of the world, a grid
        over the floor plan in a few layers of height: each lists the lamps
        whose reach touches it, and a fragment loops over its cluster's
        list instead of over every lamp in the frame. The
        grid is rebuilt only when the block's lamps change, and is left out
        where it would not pay: few lamps, or a cell more lamps reach than
        a list holds (the plain loop then, which is always right)."""
        location = uniforms['uLightGridParams']
        if location == -1:
            return
        count = int(getattr(self, '_light_ubo_count', 0) or 0)
        key = self._light_ubo_key
        params = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        if count >= self.FORWARD_PLUS_MIN_LIGHTS and key:
            if key != self._env_grid_key:
                self._env_grid_key = key
                # The block's key changes with what the grid does not use
                # (a streamed lamp's fade, its cookie): rebuilt only when the
                # lamps' places, reach or order do.
                data = self._light_ubo_data[:count]
                shape = (count, data['position'][:, :3].tobytes(),
                         data['params'][:, 1].tobytes())
                if shape != self._env_grid_shape:
                    self._env_grid_shape = shape
                    self._env_grid_params = self._env_build_light_grid(count)
            params = self._env_grid_params
        if self._env_grid_sent.get(shader_name) != params:
            gl_state.glUniform4f(location, *params[:4])
            gl_state.glUniform2f(uniforms['uLightGridHeight'], params[4], params[5])
            self._env_grid_sent[shader_name] = params
        self.light_grid_stats['on'] = params[3] > 0.5

    @staticmethod
    def light_grid(positions, radii):
        """Bin lamps (``(n, 3)`` positions, ``(n,)`` radii) into clusters:
        ``(table, corner (x, y, z), cell size, layer height)``, or None when
        a cluster is reached by more lamps than a list holds. *table* is
        ``(layers * cells * cells, depth)`` uint8: each row a cluster's lamp
        indices in ascending order, 255 after the last; a cluster's row is
        ``(layer * cells + z) * cells + x``. Pure numpy; no GL."""
        cells, layers = surface.LIGHT_GRID_CELLS, surface.LIGHT_GRID_LAYERS
        depth = surface.LIGHT_GRID_DEPTH
        positions = np.asarray(positions, dtype=np.float64)
        radii = np.asarray(radii, dtype=np.float64)
        low = (positions - radii[:, None]).min(axis=0)
        high = (positions + radii[:, None]).max(axis=0)
        # Square cells over the floor plan; the far edges fall inside.
        size = max(high[0] - low[0], high[2] - low[2], 1.0) / cells * 1.0001
        height = max(high[1] - low[1], 1.0) / layers * 1.0001

        def touches(axis, count, step):
            # A lamp reaches a slab when its box of reach overlaps it.
            edges = np.arange(count + 1) * step + low[axis]
            return ((edges[None, 1:] > (positions[:, axis] - radii)[:, None])
                    & (edges[None, :-1] < (positions[:, axis] + radii)[:, None]))

        reach = (touches(1, layers, height)[:, :, None, None]
                 & touches(2, cells, size)[:, None, :, None]
                 & touches(0, cells, size)[:, None, None, :])      # (lamp, y, z, x)
        if int(reach.sum(axis=0).max()) > depth:
            return None
        table = np.full((layers * cells * cells, depth), 255, dtype=np.uint8)
        lamp, y, z, x = np.nonzero(reach)
        row = (y * cells + z) * cells + x
        order = np.lexsort((lamp, row))
        row, lamp = row[order], lamp[order]
        starts = np.searchsorted(row, row, side='left')
        table[row, np.arange(len(row)) - starts] = lamp
        return table, (float(low[0]), float(low[1]), float(low[2])), size, height

    def _env_build_light_grid(self, count):
        """The grid for the first *count* lamps of the light block, as a
        texture; returns the program's uLightGridParams."""
        data = self._light_ubo_data[:count]
        stats = self.light_grid_stats
        stats['lamps'] = count
        built = self.light_grid(data['position'][:, :3], data['params'][:, 1]) \
            if count <= 255 else None
        if built is None:
            return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        table, corner, size, height = built
        listed = (table != 255).sum(axis=1)
        stats['builds'] += 1
        stats['max_per_cell'] = int(listed.max())
        stats['mean_per_cell'] = float(listed[listed > 0].mean()) if listed.any() else 0.0
        created = not self._env_grid_tex
        if created:
            self._env_grid_tex = int(gl.glGenTextures(1))
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.LIGHT_GRID_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_grid_tex)
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 1)
        if created:
            # The table's shape is fixed (surface.LIGHT_GRID_*): made once,
            # its contents replaced after.
            gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_R8UI, table.shape[1], table.shape[0],
                            0, gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, table.tobytes())
            for name in (gl.GL_TEXTURE_MIN_FILTER, gl.GL_TEXTURE_MAG_FILTER):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, name, gl.GL_NEAREST)
            for name in (gl.GL_TEXTURE_WRAP_S, gl.GL_TEXTURE_WRAP_T):
                gl.glTexParameteri(gl.GL_TEXTURE_2D, name, gl.GL_CLAMP_TO_EDGE)
        else:
            gl.glTexSubImage2D(gl.GL_TEXTURE_2D, 0, 0, 0, table.shape[1], table.shape[0],
                               gl.GL_RED_INTEGER, gl.GL_UNSIGNED_BYTE, table.tobytes())
        gl.glPixelStorei(gl.GL_UNPACK_ALIGNMENT, 4)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        return (corner[0], corner[2], 1.0 / size, 1.0, corner[1], 1.0 / height)

    def hd_memory(self):
        """Bytes of video memory HD_High holds beyond what a classic frame
        needs, by what it is for (tools/bench_hd.py). Counted from the
        targets' sizes and formats; drivers may round up."""
        width, height = self._postfx_size
        pixels = width * height
        half = max(1, width // 2) * max(1, height // 2)
        bloom = sum(w * h * 4 for _t, _f, w, h in self._postfx_bloom)
        frame = (pixels * 8 * 4 // 3      # colour, RGBA16F with mips
                 + pixels * 8             # normal, sky light, roughness
                 + pixels * 4             # depth and stencil
                 + pixels * 8             # resolved
                 + pixels * 4             # display
                 + half * 3) if pixels else 0
        shadow = 2 * self._env_shadow_size ** 2 * 4 if self._env_shadow_tex else 0
        grid = self._env_skylight_grid
        cover = (grid.shape[0] * grid.shape[1] * grid.shape[2] * 4
                 if self._env_skylight_tex and grid is not None else 0)
        env = ENV_MAP_SIZE[0] * ENV_MAP_SIZE[1] * 8 * 4 // 3 if self._env_map else 0
        if self._env_reflect_map:
            env += env // 4
        maps = 0
        for found in self._material_maps.values():
            if not found:
                continue
            for texture in found[:2]:
                gl_state.glBindTexture(gl.GL_TEXTURE_2D, texture)
                w = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_WIDTH)
                h = gl.glGetTexLevelParameteriv(gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_HEIGHT)
                maps += int(w) * int(h) * 4 * 4 // 3
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, 0)
        # The point lights' shadow array (lighting.py): six 16-bit layers a
        # slot, every slot at the largest light's size.
        point = 0
        if getattr(self, '_shadow_cubemaps', None) and self._shadow_array_size > 1:
            point = 6 * self._shadow_array_slots * self._shadow_array_size ** 2 * 2
        return {'frame targets': frame, 'bloom': bloom, 'sun shadow maps': shadow,
                'point shadow maps': point,
                'sky light volume': cover, 'environment maps': env, 'material maps': maps}

    def _bind_terrain_layers(self, terrain_gl):
        """The terrain layers' normal maps (the companions of its sand,
        grass, rock and snow textures) on their units, and each layer's
        roughness, for the terrain program in an HD frame."""
        if not self._env_active:
            return
        uniforms = self.uniforms.get('terrain')
        if uniforms is None or uniforms['uLayerMaps'] == -1:
            return
        bound, rough = [], []
        for index, name in enumerate(('sand_tex', 'grass_tex', 'rock_tex', 'snow_tex')):
            texture = int(getattr(terrain_gl, name, 0) or 0)
            maps = self._material_for(texture) if texture else None
            gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.TERRAIN_NORMAL_UNIT + index)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, maps[0] if maps else 0)
            bound.append(1.0 if maps else 0.0)
            rough.append((0.92, 0.9, 0.82, 0.6)[index])    # sand, grass, rock, snow
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        gl_state.glUniform4f(uniforms['uLayerMaps'], *bound)
        gl_state.glUniform4f(uniforms['uLayerRough'], *rough)

    def _model_surface(self, shader_name, surface):
        """A model draw group's own ``(roughness, metalness)``, or None for
        the environment's (no-op off an environment frame)."""
        if not self._env_active:
            return
        current = self._env_surface_default.get(shader_name)
        if current == surface:
            return
        location = self.uniforms[shader_name]['uSurfaceDefault']
        if location == -1:
            return
        env = self.environment
        if surface is None:
            gl_state.glUniform4f(location, env.roughness, env.metalness, 0.0, 0.0)
            del self._env_surface_default[shader_name]
        else:
            gl_state.glUniform4f(location, surface[0], surface[1], 0.0, 0.0)
            self._env_surface_default[shader_name] = surface

    # ------------------------------------------------------------------
    # Material maps
    # ------------------------------------------------------------------
    def _material_for(self, texture):
        """A diffuse texture's companion maps: ``(normal, material,
        parallax, emission)`` as GL names and scalars, or None for a
        texture that is flat by intent. Resolved once per texture:

        * beside its file (png, webp or jpg): ``<name>_n`` -- the normal map
          (OpenGL convention) with the surface's height in its alpha, or in
          a separate ``<name>_h`` -- and ``<name>_m`` -- ambient occlusion,
          roughness and metalness in R, G and B, and 1 - emission in alpha;
          ``<name>.mat.json`` gives ``parallax``, the depth of the relief in
          world units (scaled by the environment's ``parallax``), and
          ``emission``, a gain on the map's emission;
        * a texture with no such files gets them made from its own picture
          by the same generator the stock maps were baked with
          (engine.material_maps), off this thread, and kept on disk by the
          picture's content: nothing is drawn flat for want of maps;
        * tool textures (nodraw, trigger, Dev/ grids...) and what is not a
          surface at all (light cookies, effect sprites, water's own normal
          map) have none, on purpose."""
        try:
            return self._material_maps[texture]
        except KeyError:
            pass
        loading = self._material_loading.get(texture)
        if loading is not None:
            # Being made or decoded: only the job is asked, not the files.
            return self._material_collect(texture, loading)
        from engine import material_maps
        from engine.asset_archives import asset_bytes, asset_exists
        from engine.asset_paths import confined_path
        if len(self._material_names) != len(self.texture_manager):
            self._material_names = {int(v): k for k, v in self.texture_manager.items()}
        found = None
        key = self._material_names.get(int(texture))
        absolute = None
        if key and key.startswith('model_tex:'):
            # A model's own texture, cached by its resolved path.
            path = key[len('model_tex:'):].replace(os.sep, '/')
            if path.startswith('assets/'):
                key = path[len('assets/'):]
            else:
                key, absolute = None, path
        if key is None and absolute is None:
            self._material_maps[texture] = None
            return None
        relative = (key[len('textures/'):] if key and key.startswith('textures/')
                    else os.path.basename(key or absolute))
        if key and key.startswith('textures/') and (
                material_maps.is_tool_texture(relative)
                or not material_maps.is_surface_texture(relative)):
            self._material_maps[texture] = None
            return None
        parallax, emission = 0.0, 1.0
        if key:
            base = os.path.splitext(key)[0]

            def companion(suffix):
                for extension in ('.png', '.webp', '.jpg'):
                    path = confined_path('assets', base + suffix + extension)
                    if path is not None and asset_exists(path):
                        return path
                return None

            sidecar = confined_path('assets', base + '.mat.json')
            values = {}
            if sidecar is not None and asset_exists(sidecar):
                try:
                    values = json.loads(asset_bytes(sidecar).decode('utf-8'))
                except Exception as exc:
                    print(f"[Environment] {sidecar}: {exc}")
            normal_path, material_path = companion('_n'), companion('_m')
            if normal_path and material_path:
                height_path = companion('_h')
                # Without a height there is nothing for parallax to follow.
                has_height = bool(height_path) or normal_path.lower().endswith(('.png', '.webp'))
                try:
                    parallax = float(values.get('parallax', 1.0 if has_height else 0.0))
                    emission = float(values.get('emission', 1.0))
                except (TypeError, ValueError):
                    parallax, emission = 0.0, 1.0
                if not has_height:
                    parallax = 0.0
                return self._material_decode(texture, (normal_path, height_path),
                                             (material_path, None), parallax, emission)
        # Made from the picture itself, by the generator, on another thread.
        self._material_loading[texture] = ('made', key, absolute)
        return self._material_collect(texture, self._material_loading[texture])

    #: Material maps uploaded at most this many textures a frame: each is
    #: two mip-mapped images, and a map coming into view must not stall the
    #: frame it comes into view in (a surface draws flat until its maps are in).
    MATERIAL_UPLOADS_PER_FRAME = 2

    def _material_executor(self):
        pool = self.__dict__.get('_material_pool')
        if pool is None:
            from concurrent.futures import ThreadPoolExecutor
            pool = self.__dict__['_material_pool'] = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix='fio-materials')
        return pool

    def _material_decode(self, texture, normal, material, parallax, emission):
        """Start decoding a texture's two maps off this thread (the files
        are resolved here, the pixels decoded there); None meanwhile."""
        try:
            sources = [(path, data_image_source(path),
                        alpha, data_image_source(alpha) if alpha else None)
                       for path, alpha in (normal, material)]
        except Exception as exc:
            print(f"[Environment] could not load {normal[0]}: {exc}")
            self._material_maps[texture] = None
            return None
        pool = self._material_executor()
        jobs = [pool.submit(decode_data_image, *args) for args in sources]
        self._material_loading[texture] = ('decode', jobs, parallax, emission)
        return self._material_collect(texture, self._material_loading[texture])

    def _material_collect(self, texture, loading):
        """Advance a texture's maps that are being made or decoded: the
        found maps once they are uploaded, None until then."""
        if loading[0] == 'made':
            made = self._material_generated(texture, loading[1], loading[2])
            if made is None:
                return None             # still being made: asked again next time
            del self._material_loading[texture]
            if not made:
                self._material_maps[texture] = None
                return None
            # A guessed height is not worth relief: no parallax.
            return self._material_decode(texture, (made[0], None), (made[1], None), 0.0, 1.0)
        _kind, jobs, parallax, emission = loading
        if not all(job.done() for job in jobs) or self._material_uploads_left <= 0:
            return None
        self._material_uploads_left -= 1
        del self._material_loading[texture]
        decoded = []
        for job in jobs:
            try:
                decoded.append(job.result())
            except Exception:
                decoded.append(None)
        found = None
        if all(decoded):
            anisotropy = self._env_max_anisotropy()
            normal = upload_data_texture(decoded[0], anisotropy)
            material = upload_data_texture(decoded[1], anisotropy)
            # Loading put the maps on the diffuse texture's unit: give it back.
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, int(texture))
            if normal and material:
                found = (normal, material, parallax, emission)
                _set_anisotropy(anisotropy)     # the diffuse was loaded without it
            else:
                stray = [t for t in (normal, material) if t]
                if stray:
                    gl_state.glDeleteTextures(stray)
        self._material_maps[texture] = found
        return found

    def _material_generated(self, texture, key, absolute):
        """The generated maps of a texture that has none beside it:
        ``(normal path, material path)`` in the on-disk cache, ``()`` if they
        could not be made, or None while a worker thread is still making
        them (numpy and file work only; no GL off this thread)."""
        pending = self.__dict__.setdefault('_material_pending', {})
        job = pending.get(texture)
        if job is None:
            from engine import material_maps
            from engine.asset_archives import asset_bytes
            from engine.asset_paths import confined_path
            try:
                if key:
                    data = asset_bytes(confined_path('assets', key))
                else:
                    with open(absolute, 'rb') as handle:
                        data = handle.read()
            except Exception:
                return ()
            if not data:
                return ()
            pending[texture] = self._material_executor().submit(
                material_maps.ensure_cached, data)
            self.material_stats['generated'] += 1
            return None
        if not job.done():
            return None
        del pending[texture]
        try:
            return job.result() or ()
        except Exception:
            return ()

    def _bind_material_maps(self, shader_name, texture):
        """After a brush pass binds diffuse *texture* on unit 0: bind its
        companion maps and tell the program (no-op off an environment frame)."""
        if not self._env_active:
            return
        maps = self._material_for(texture)
        if maps is not None and self._env_units_material is not maps:
            gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.NORMAL_MAP_UNIT)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, maps[0])
            gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.MATERIAL_MAP_UNIT)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, maps[1])
            gl_state.glActiveTexture(gl.GL_TEXTURE0)
            self._env_units_material = maps
        # w: the diffuse has no alpha channel, so the depth prepass need not
        # sample it (surface._TEXTURED_MAIN).
        count = len(self.texture_manager)
        if count != self._texture_opaque_count:
            # Textures were loaded or freed: a GL name may now be another
            # picture's.
            self._texture_opaque.clear()
            self._texture_opaque_count = count
        opaque = self._texture_opaque.get(texture)
        if opaque is None:
            # Once a texture, while it is bound on unit 0 (the pass just bound it).
            try:
                opaque = int(gl.glGetTexLevelParameteriv(
                    gl.GL_TEXTURE_2D, 0, gl.GL_TEXTURE_ALPHA_SIZE)) == 0
            except Exception:
                opaque = False
            self._texture_opaque[texture] = opaque
        if maps is None:
            value = (0.0, 0.0, 0.0, 1.0 if opaque else 0.0)
        else:
            env = self.environment
            value = (1.0, env.parallax * maps[2], env.emissive_gain * maps[3],
                     1.0 if opaque else 0.0)
        # By value: most textures' maps give the same vector, and a run of
        # them then sends it once.
        if self._env_material_bound.get(shader_name) == value:
            return
        uniforms = self.uniforms.get(shader_name)
        if uniforms is None:
            return
        location = uniforms['uMaterial']
        if location == -1:
            return
        gl_state.glUniform4f(location, *value)
        self._env_material_bound[shader_name] = value

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------
    def _cleanup_environment(self):
        pool = self.__dict__.pop('_material_pool', None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        self.__dict__.pop('_material_pending', None)
        self._material_loading = {}
        self._texture_opaque = {}
        self._cleanup_postfx()
        textures = [t for t in (self._env_shadow_tex, self._env_shadow_static_tex,
                                self._env_noise_tex, self._env_map,
                                self._env_reflect_map, self._env_skylight_tex,
                                self._env_skylight_blank,
                                self._env_grid_tex) if t]
        self._env_grid_tex = 0
        self._env_grid_key = None
        self._env_grid_shape = None
        self._env_grid_sent = {}
        for maps in self._material_maps.values():
            if maps:
                textures += [maps[0], maps[1]]
        if textures:
            gl_state.glDeleteTextures(textures)
        framebuffers = [f for f in (self._env_shadow_fbo, self._env_shadow_static_fbo,
                                    self._env_map_fbo) if f]
        self._env_skylight_tex = self._env_skylight_blank = 0
        self._env_skylight_sig = None
        self._env_skylight_grid = None
        if self._hdr_ubo:
            gl.glDeleteBuffers(1, [self._hdr_ubo])
            self._hdr_ubo = 0
            self._hdr_state = None
        if framebuffers:
            gl.glDeleteFramebuffers(len(framebuffers), framebuffers)
        for program in self._env_programs.values():
            if program is not None:
                program.delete()
        if self._env_vao:
            gl_state.glDeleteVertexArrays(1, [self._env_vao])
        self._env_programs = {}
        self._material_maps = {}
        self._env_shadow_tex = self._env_noise_tex = self._env_map = 0
        self._env_shadow_static_tex = self._env_reflect_map = 0
        self._env_shadow_fbo = self._env_shadow_static_fbo = 0
        self._env_map_fbo = self._env_vao = 0
        self._env_shadow_size = 0
        self._env_shadow_static_sig = None
        self._env_map_key = None
        self._env_reflect_key = None
        self._env_surface_state.clear()
        self._env_active = False
