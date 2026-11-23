"""Physically based surface shading for the forward renderer's lit programs.

``lit.frag`` and ``textured.frag`` (engine.shaders) light a surface with a
Lambert term per point light, in display space. That is the look every Fio
map was built with, and it stays the default. A renderer whose
:class:`~engine.renderer.hd_high.environment.Environment` is switched on
shades the same surfaces physically instead:

* albedo decoded to linear light, a GGX specular lobe and energy-conserving
  diffuse, with roughness / metalness / ambient occlusion / emission from a
  texture's companion maps (``<name>_n``: normal + height, ``<name>_m``:
  AO, roughness, metalness, emission);
* tangent-space normal mapping and parallax occlusion mapping, with the
  tangent frame taken from screen-space derivatives, so no mesh needs
  tangents;
* a sun: one directional light with a filtered shadow map, moving cloud
  shadows, and sky light from three hemisphere colours;
* aerial perspective toward the sky's own horizon colour.

Nothing in ``engine.shaders`` changes for it. :func:`surface_source` rewrites
a lit fragment shader *source*: the shader's own ``main`` becomes
``fioLegacyMain`` and a new ``main`` runs it while ``uSurfaceMode`` is 0 (the
uniform's default, so a program nobody configures renders exactly as before),
and shades physically otherwise. The Android player and the low-power ARM
shaders never see the rewrite.

The result is written for an HDR target (see ``postfx.py``): colour is
gamma-encoded but unclamped, alpha carries the surface's specular
reflectance for the reflection pass, and a second output carries the shading
normal and roughness.
"""

import re

#: Texture units of the surface samplers. Above everything the forward
#: renderer already binds (0 diffuse, 2 glass, 3 water depth, 4..11 shadow
#: cubes, 15 panorama, 16 light cookies).
SUN_SHADOW_UNIT = 17
ENV_MAP_UNIT = 18
NORMAL_MAP_UNIT = 19
MATERIAL_MAP_UNIT = 20
NOISE_UNIT = 21
SKY_LIGHT_UNIT = 22

#: The programs :func:`surface_source` is applied to (those built from
#: ``lit.frag`` / ``textured.frag``).
SURFACE_PROGRAMS = ('lit', 'textured', 'lit_instanced', 'textured_instanced',
                    'brush_instanced', 'lit_brush_instanced', 'geo_textured',
                    'geo_batched')

#: Sampler uniform -> unit, set once per program (a sampler2DShadow left on
#: unit 0 beside the diffuse sampler2D is an invalid program state).
TERRAIN_NORMAL_UNIT = 23       # .. 26: the terrain layers' normal maps
LIGHT_GRID_UNIT = 27

#: Forward+ (clustered forward shading, as Filament and Bevy do it): the
#: frame's lamps are binned on the CPU into a grid of clusters
#: (environment._env_light_grid) and a fragment loops over its cluster's
#: lamps only. The clusters are boxes of the world, not of the view --
#: LIGHT_GRID_CELLS square over the floor plan, LIGHT_GRID_LAYERS high -- so
#: the lists change when the lamps do, not whenever the camera moves. A
#: cluster lists at most LIGHT_GRID_DEPTH lamps (as indices into the light
#: block, 255 ending the list).
LIGHT_GRID_CELLS = 32
LIGHT_GRID_LAYERS = 4
LIGHT_GRID_DEPTH = 32
SURFACE_SAMPLERS = (('uSunShadow', SUN_SHADOW_UNIT), ('uEnvMap', ENV_MAP_UNIT),
                    ('uNormalMap', NORMAL_MAP_UNIT),
                    ('uMaterialMap', MATERIAL_MAP_UNIT), ('uNoise', NOISE_UNIT),
                    ('uSkyLight', SKY_LIGHT_UNIT), ('uLightGrid', LIGHT_GRID_UNIT))

#: Value noise and the cloud layer, shared by the sky and by the cloud
#: shadows the surfaces receive, so a shadow on the ground is under the cloud
#: that casts it. ``uNoise`` is a 256x256 tileable random texture.
CLOUD_GLSL = """
uniform sampler2D uNoise;
uniform vec4 uCloud;               // x coverage, y softness, z layer height, w 1 / feature size
uniform highp vec2 uCloudOffset;   // wind * time, in noise units
float fioNoise(highp vec2 p) {
    highp vec2 i = floor(p);
    highp vec2 f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return textureLod(uNoise, (i + f + 0.5) / 256.0, 0.0).r;
}
float fioFbm(highp vec2 p) {
    float total = 0.0;
    float gain = 0.5;
    for (int i = 0; i < 5; i++) {
        total += gain * fioNoise(p);
        p = mat2(1.6, 1.2, -1.2, 1.6) * p + vec2(17.3, 9.1);
        gain *= 0.5;
    }
    return total / 0.96875;
}
float fioFbmLow(highp vec2 p) {
    return (fioNoise(p) * 0.5 + fioNoise(mat2(1.6, 1.2, -1.2, 1.6) * p + vec2(17.3, 9.1)) * 0.25
            + fioNoise(mat2(0.56, 3.92, -3.92, 0.56) * p + vec2(51.9, 33.4)) * 0.125) / 0.875;
}
float fioCloudCover(float n) {
    float lo = mix(0.80, 0.22, uCloud.x);
    return smoothstep(lo, lo + uCloud.y, n);
}
// Density of the cloud layer above the world point (x, z) *q*.
float fioCloudDensity(highp vec2 q) {
    return fioCloudCover(fioFbm(q * uCloud.w + uCloudOffset));
}
float fioCloudDensityLow(highp vec2 q) {
    return fioCloudCover(fioFbmLow(q * uCloud.w + uCloudOffset));
}
"""

#: Equirectangular lookup of the environment map (the sky as the renderer
#: drew it, mip-mapped). Always sampled with an explicit level: the atan
#: seam makes implicit derivatives useless.
ENV_GLSL = """
const float FIO_PI = 3.14159265358979;
highp vec2 fioEnvUV(highp vec3 d) {
    return vec2(atan(d.x, -d.z) / (2.0 * FIO_PI) + 0.5,
                0.5 - asin(clamp(d.y, -1.0, 1.0)) / FIO_PI);
}
vec3 fioToLinear(vec3 c) { return pow(max(c, vec3(0.0)), vec3(2.2)); }
// A unit vector in two numbers (octahedral), 0..1 each.
vec2 fioPackNormal(vec3 n) {
    n /= (abs(n.x) + abs(n.y) + abs(n.z));
    vec2 o = n.z >= 0.0 ? n.xy : (1.0 - abs(n.yx)) * vec2(n.x >= 0.0 ? 1.0 : -1.0,
                                                          n.y >= 0.0 ? 1.0 : -1.0);
    return o * 0.5 + 0.5;
}
vec3 fioUnpackNormal(vec2 p) {
    vec2 o = p * 2.0 - 1.0;
    vec3 n = vec3(o, 1.0 - abs(o.x) - abs(o.y));
    float t = max(-n.z, 0.0);
    n.x += n.x >= 0.0 ? -t : t;
    n.y += n.y >= 0.0 ? -t : t;
    return normalize(n);
}
vec3 fioEncode(vec3 c) { return pow(max(c, vec3(0.0)), vec3(1.0 / 2.2)); }
// Split-sum environment BRDF, Karis' mobile approximation.
vec3 fioEnvBRDF(vec3 f0, float rough, float nov) {
    vec4 c0 = vec4(-1.0, -0.0275, -0.572, 0.022);
    vec4 c1 = vec4(1.0, 0.0425, 1.04, -0.04);
    vec4 r = rough * c0 + c1;
    float a004 = min(r.x * r.x, exp2(-9.28 * nov)) * r.x + r.y;
    vec2 ab = vec2(-1.04, 1.04) * a004 + r.zw;
    return f0 * ab.x + ab.y;
}
"""

SURFACE_GLSL = """
// ---- physically based surfaces (engine/renderer/hd_high/surface.py) ----
uniform int uSurfaceMode;          // 0 legacy, 1 opaque phase, 2 blended phase, 3 opaque in a portal's view
uniform highp vec3 uSunDir;        // toward the sun
uniform vec3 uSunColor;
uniform highp mat4 uSunMatrix;     // world -> shadow clip space
uniform highp sampler2DShadow uSunShadow;
uniform vec4 uSunShadowParams;     // x 1 / size, y size, z normal offset, w strength
uniform sampler2D uEnvMap;
uniform vec4 uEnvParams;           // x top mip, y sky specular here (1) or in the reflection pass (0), z emissive gain, w point light gain
uniform vec3 uAmbientSky;
uniform vec3 uAmbientHorizon;
uniform vec3 uAmbientGround;
uniform sampler2D uNormalMap;
uniform sampler2D uMaterialMap;
uniform vec4 uMaterial;            // x maps bound, y parallax depth (world units), z emission of the map, w unused
uniform vec4 uSurfaceDefault;      // x roughness, y metalness, z detail strength, w unused
uniform vec4 uAtmosphere;          // x haze density, y haze height falloff, z cloud shadow strength, w unused
uniform vec4 uEnvParams2;          // x the environment map is a probe's whole sphere, y haze colour from uHazeColor, z gain on the environment map
uniform highp sampler3D uSkyLight; // the sky light volume (skylight.py): r light, g whether the cell is open
uniform highp vec4 uSkyLightOrigin; // xyz the volume's corner (world), w normal offset (world)
uniform highp vec4 uSkyLightScale;  // xyz 1 / the volume's extent, w 0 off, 1 on, 2 on and a window on a larger map
uniform highp usampler2D uLightGrid;   // Forward+: row = cluster, column k = the cluster's k-th lamp (255: no more)
uniform vec4 uLightGridParams;     // x, z of the grid's corner, 1 / cell size, 1 while the grid is in use
uniform vec2 uLightGridHeight;     // y of the grid's bottom, 1 / layer height
uniform vec3 uHazeColor;
uniform vec3 uReflectIndoor;       // what a reflection finds where no sky light reaches at all
uniform vec3 uReflectFallback;     // ... and under the horizon of a sky, where walls would be
uniform vec4 uHdrFog;              // the map's distance fog colour, in the frame's units; a 1 while it is on
layout(location = 1) out vec4 FragData;
// The sun a surface is lit by: the frame's (fioSunBegin), unless a program
// sets its own before fioShade (the terrain, under a sky with no sun).
highp vec3 fioSunDir;
vec3 fioSunColor;
float fioSunMapped;                // 1: the sun's shadow map applies
void fioSunBegin() { fioSunDir = uSunDir; fioSunColor = uSunColor; fioSunMapped = 1.0; }
""" + ENV_GLSL + CLOUD_GLSL + """
float fioSunTap(highp vec2 base, float u, float v, highp float z) {
    return texture(uSunShadow, vec3(base + vec2(u, v) * uSunShadowParams.x, z));
}
// 1 lit .. 0 shadowed: a 5x5 tent filter in nine bilinear comparisons.
float fioSunShadow(highp vec3 pos, vec3 ng) {
    if (uSunShadowParams.w <= 0.0) return 1.0;
    highp vec4 clip = uSunMatrix * vec4(pos + ng * uSunShadowParams.z, 1.0);
    highp vec3 s = clip.xyz * 0.5 + 0.5;
    if (s.x <= 0.0 || s.x >= 1.0 || s.y <= 0.0 || s.y >= 1.0 || s.z >= 1.0) return 1.0;
    highp vec2 uv = s.xy * uSunShadowParams.y;
    highp vec2 base = floor(uv + 0.5);
    float fx = uv.x + 0.5 - base.x;
    float fy = uv.y + 0.5 - base.y;
    base = (base - 0.5) * uSunShadowParams.x;
    float uw0 = 4.0 - 3.0 * fx;
    float uw1 = 7.0;
    float uw2 = 1.0 + 3.0 * fx;
    float u0 = (3.0 - 2.0 * fx) / uw0 - 2.0;
    float u1 = (3.0 + fx) / uw1;
    float u2 = fx / uw2 + 2.0;
    float vw0 = 4.0 - 3.0 * fy;
    float vw1 = 7.0;
    float vw2 = 1.0 + 3.0 * fy;
    float v0 = (3.0 - 2.0 * fy) / vw0 - 2.0;
    float v1 = (3.0 + fy) / vw1;
    float v2 = fy / vw2 + 2.0;
    float lit = uw0 * vw0 * fioSunTap(base, u0, v0, s.z)
              + uw1 * vw0 * fioSunTap(base, u1, v0, s.z)
              + uw2 * vw0 * fioSunTap(base, u2, v0, s.z)
              + uw0 * vw1 * fioSunTap(base, u0, v1, s.z)
              + uw1 * vw1 * fioSunTap(base, u1, v1, s.z)
              + uw2 * vw1 * fioSunTap(base, u2, v1, s.z)
              + uw0 * vw2 * fioSunTap(base, u0, v2, s.z)
              + uw1 * vw2 * fioSunTap(base, u1, v2, s.z)
              + uw2 * vw2 * fioSunTap(base, u2, v2, s.z);
    return mix(1.0, lit / 144.0, uSunShadowParams.w);
}
// How much of the sky's light reaches a point, 0 in a closed room .. 1 in
// the open: all of it where nothing is overhead, and what spreads in through
// the windows and doors indoors (the sky light volume, skylight.py). Sky
// light is scaled by it. The point is first moved off its surface
// (uSkyLightOrigin.w), into the room it faces; the filtered light is divided
// by the filtered share of open cells, which leaves the cells a wall fills
// out of the filter, so a wall is lit by its own side.
float fioSkyVisible(highp vec3 pos, vec3 ng) {
    if (uSkyLightScale.w <= 0.0) return 1.0;
    highp vec3 q = (pos + ng * uSkyLightOrigin.w - uSkyLightOrigin.xyz) * uSkyLightScale.xyz;
    // Beside the volume or above it there is only sky (below it, its floor).
    if (q.x <= 0.0 || q.x >= 1.0 || q.z <= 0.0 || q.z >= 1.0 || q.y >= 1.0) return 1.0;
    vec2 s = textureLod(uSkyLight, q, 0.0).rg;
    float open = clamp(s.r / max(s.g, 1e-4), 0.0, 1.0) * smoothstep(FIO_OPEN_LOW, FIO_OPEN_HIGH, s.g);
    if (uSkyLightScale.w > 1.5) {
        // A window that follows the view: eased into the open at its rim.
        highp vec2 rim = min(q.xz, 1.0 - q.xz);
        open = mix(1.0, open, clamp(min(rim.x, rim.y) * 12.0, 0.0, 1.0));
    }
    return open;
}
float fioCloudShadow(highp vec3 pos) {
    if (uAtmosphere.z <= 0.0) return 1.0;
    highp vec2 q = pos.xz + uSunDir.xz * ((uCloud.z - pos.y) / max(uSunDir.y, 0.15));
    return 1.0 - fioCloudDensityLow(q) * uAtmosphere.z;
}
// Diffuse plus GGX specular for one light, times N.L. *radiance* is the
// light's contribution to a white diffuse surface facing it.
vec3 fioLight(vec3 n, vec3 v, vec3 l, vec3 diffuse, vec3 f0, float rough, vec3 radiance) {
    float nol = max(dot(n, l), 0.0);
    if (nol <= 0.0) return vec3(0.0);
    vec3 h = normalize(v + l);
    float nov = max(dot(n, v), 1e-3);
    float noh = max(dot(n, h), 0.0);
    float voh = max(dot(v, h), 0.0);
    float a = max(rough * rough, 0.003);
    float a2 = a * a;
    float dd = noh * noh * (a2 - 1.0) + 1.0;
    float ndf = a2 / (FIO_PI * dd * dd);
    float vis = 0.5 / (nol * sqrt(nov * nov * (1.0 - a2) + a2)
                       + nov * sqrt(nol * nol * (1.0 - a2) + a2) + 1e-5);
    vec3 fres = f0 + (1.0 - f0) * pow(1.0 - voh, 5.0);
    // Highlights from a sun are small and very bright: kept below what the
    // bloom can swallow without fireflies.
    vec3 spec = min(ndf * vis * FIO_PI, 60.0) * fres;
    return (diffuse * (1.0 - fres) + spec) * radiance * nol;
}
// Steep parallax with a linear refinement: the texture coordinate the view
// ray reaches in the height field of uNormalMap's alpha.
highp vec2 fioParallax(highp vec2 uv, highp vec2 dx, highp vec2 dy, highp vec2 reach, float nov) {
    // A step a texel of the height map is as fine as the map can tell
    // apart: a relief that moves the lookup three texels takes four steps,
    // not twenty, and one that moves it less than half a texel takes none.
    highp vec2 texels = abs(reach) * vec2(textureSize(uNormalMap, 0));
    float span = max(texels.x, texels.y);
    if (span < 0.5) return uv;
    float count = min(mix(20.0, 8.0, nov), max(ceil(span), 4.0));
    float layer = 1.0 / count;
    highp vec2 delta = reach * layer;
    float depth = 0.0;
    float here = 1.0 - textureGrad(uNormalMap, uv, dx, dy).a;
    float before = here;
    for (int i = 0; i < 20; i++) {
        if (depth >= here) break;
        uv += delta;
        depth += layer;
        before = here;
        here = 1.0 - textureGrad(uNormalMap, uv, dx, dy).a;
    }
    float under = depth - here;
    float over = before - (depth - layer);
    return uv - delta * clamp(under / max(under + over, 1e-5), 0.0, 1.0);
}
// Light a surface. *src* is the surface's own opacity. Defined below main
// (SURFACE_SHADE_GLSL): engine.shaders.light_ubo_source rewrites light loops
// and then writes into main() at the place it found it first, so nothing
// with a light loop may stand above main.
void fioShade(vec3 albedo, vec3 n, vec3 ng, float rough, float metal, float occlusion,
              vec3 emission, float src);
void fioLegacyMain();
"""

from engine.renderer.hd_high import skylight as _skylight
SURFACE_GLSL = (SURFACE_GLSL.replace('FIO_OPEN_LOW', repr(_skylight.OPEN_LOW))
                .replace('FIO_OPEN_HIGH', repr(_skylight.OPEN_HIGH)))

SURFACE_SHADE_GLSL = """
// Light a surface. *src* is the surface's own opacity.
void fioShade(vec3 albedo, vec3 n, vec3 ng, float rough, float metal, float occlusion,
              vec3 emission, float src) {
    highp vec3 toEye = uFogCamPos - FragPos;
    highp float eyeDist = length(toEye);
    vec3 v = toEye / max(eyeDist, 1e-4);
    rough = clamp(rough, 0.04, 1.0);
    // A normal map finer than the pixels would sparkle under a lamp: widen
    // the highlight by how much the normal varies across the pixel.
    vec3 nx = dFdx(n);
    vec3 ny = dFdy(n);
    float spread = min(2.0 * 0.25 * (dot(nx, nx) + dot(ny, ny)), 0.36);
    rough = sqrt(clamp(rough * rough + spread, 0.0, 1.0));
    vec3 f0 = mix(vec3(0.04), albedo, metal);
    vec3 diffuse = albedo * (1.0 - metal);
    float nov = max(dot(n, v), 1e-3);

    // Sky light: three colours of the hemisphere, by the normal's elevation.
    float skyOpen = fioSkyVisible(FragPos, ng);
    vec3 sky = (n.y >= 0.0 ? mix(uAmbientHorizon, uAmbientSky, n.y)
                           : mix(uAmbientHorizon, uAmbientGround, -n.y)) * skyOpen;
    // The map's own ambient is classic Fio's: a tenth, plus what the map adds.
    vec3 result = diffuse * (sky + fioToLinear(vec3(0.1) + uAmbient)) * occlusion;

    // The sun, behind the geometry's own horizon as well as the shadow map's.
    float facing = dot(ng, fioSunDir);
    if (facing > 0.0 && fioSunColor.r + fioSunColor.g + fioSunColor.b > 0.0) {
        float lit = smoothstep(0.0, 0.08, facing);
        if (fioSunMapped > 0.5) lit *= fioSunShadow(FragPos, ng) * fioCloudShadow(FragPos);
        if (lit > 0.0)
            result += fioLight(n, v, fioSunDir, diffuse, f0, rough, fioSunColor) * lit
                    * mix(1.0, occlusion, 0.5);
    }
    // Forward+: with the light grid in use, only the lamps that reach this
    // fragment's cell; otherwise every lamp of the frame, as classic does.
    int fioLamps = active_lights < FIO_MAX_LIGHTS ? active_lights : FIO_MAX_LIGHTS;
    bool fioGrid = uLightGridParams.w > 0.5;
    int fioRow = 0;
    if (fioGrid) {
        highp vec2 cell = (FragPos.xz - uLightGridParams.xy) * uLightGridParams.z;
        highp float layer = (FragPos.y - uLightGridHeight.x) * uLightGridHeight.y;
        if (cell.x < 0.0 || cell.y < 0.0 || layer < 0.0 || cell.x >= FIO_GRID_CELLS.0
                || cell.y >= FIO_GRID_CELLS.0 || layer >= FIO_GRID_LAYERS.0) {
            fioLamps = 0;                   // beyond every lamp's reach
        } else {
            fioRow = (int(layer) * FIO_GRID_CELLS + int(cell.y)) * FIO_GRID_CELLS + int(cell.x);
            fioLamps = FIO_GRID_DEPTH;
        }
    }
    for (int k = 0; k < fioLamps; k++) {
        int i = k;
        if (fioGrid) {
            i = int(texelFetch(uLightGrid, ivec2(k, fioRow), 0).r);
            if (i >= active_lights) break;  // the end of the cell's list (255)
        }
        highp vec3  toLight  = lights[i].position - FragPos;
        highp float distSq   = dot(toLight, toLight);
        highp float radiusSq = lights[i].radius * lights[i].radius;
        if (distSq < radiusSq) {
            highp float dist = sqrt(distSq);
            vec3 l = toLight / dist;
            // Turned away from the lamp: nothing to add, and no shadow
            // worth its taps.
            if (dot(n, l) <= 0.0) continue;
            float att = 1.0 - (dist / lights[i].radius);
            att = att * att;
            float shadow = calcPointShadow(lights[i].shadowIndex, toLight, lights[i].radius,
                                           max(dot(ng, l), 0.0));
            // A lamp's level (intensity * falloff) is a display quantity in
            // classic Fio -- it multiplies the texture as shown -- so it is
            // decoded like a colour. The light then falls off across a room
            // exactly as the map's author saw it, instead of reaching much
            // further in linear light.
            vec3 radiance = fioToLinear(lights[i].color)
                          * (pow(lights[i].intensity * att, 2.2) * uEnvParams.w);
            result += (1.0 - shadow) * fioLight(n, v, l, diffuse, f0, rough, radiance);
        }
    }
    // With a reflection pass (postfx.py), a surface smooth enough to show a
    // picture is left to it: it traces the frame for what the surface
    // reflects. A rougher one reflects no picture, only the environment's
    // light, and takes it here -- the pass then has nothing to do for the
    // pixel, which is most pixels of most frames.
    bool solid = uSurfaceMode == 1 && src >= 0.999;
    bool traced = uEnvParams.y < 0.5 && solid && rough < 0.5;
    if (uEnvParams.y < 0.5) {
        if (solid && !traced) {
            vec3 r = reflect(-v, n);
            vec3 seen = mix(uReflectFallback,
                            textureLod(uEnvMap, fioEnvUV(r), rough * uEnvParams.x).rgb * uEnvParams2.z,
                            uEnvParams2.x > 0.5 ? 1.0 : smoothstep(-0.05, 0.45, r.y));
            if (uEnvParams2.x < 0.5) seen = mix(uReflectIndoor, seen, skyOpen);
            result += seen * fioEnvBRDF(f0, rough, nov) * mix(1.0, occlusion, 0.7);
        }
    } else {
        // No reflection pass: the sky's reflection, hidden near the horizon
        // where walls would be in the way.
        vec3 r = reflect(-v, n);
        vec3 env = textureLod(uEnvMap, fioEnvUV(r), rough * uEnvParams.x).rgb * uEnvParams2.z;
        result += env * fioEnvBRDF(f0, rough, nov) * occlusion
                * (uEnvParams2.x > 0.5 ? 1.0 : smoothstep(-0.1, 0.5, r.y) * skyOpen);
    }
    result += emission;

    // Aerial perspective, toward the sky's colour at the horizon behind.
    if (uAtmosphere.x > 0.0) {
        highp float density = uAtmosphere.x * exp(-max(FragPos.y, 0.0) * uAtmosphere.y);
        float haze = (1.0 - exp(-eyeDist * density)) * skyOpen;
        vec3 far = uEnvParams2.y > 0.5 ? uHazeColor
                 : textureLod(uEnvMap, fioEnvUV(normalize(vec3(-v.x, 0.04, -v.z))),
                              max(uEnvParams.x - 3.0, 0.0)).rgb;
        result = mix(result, far, haze);
    }
    // The map's distance fog (which replaces the haze: the renderer sends
    // one or the other).
    if (uHdrFog.a > 0.5) result = mix(result, uHdrFog.rgb, fogFactor(FragPos));
    // Alpha below 1 asks the reflection pass for this pixel, and tells it
    // the surface's reflectance.
    float marker = clamp(max(f0.r, max(f0.g, f0.b)), 0.02, 0.95);
    float outAlpha = uSurfaceMode == 2 ? src : (traced ? marker : 1.0);
    FragColor = vec4(fioEncode(result), outAlpha);
    FragData = vec4(fioPackNormal(n), skyOpen, uSurfaceMode == 1 ? rough : 1.0);
}
"""

_TEXTURED_MAIN = """
void main() {
    if (uSurfaceMode == 0) { fioLegacyMain(); return; }
    fioSunBegin();
    vec3 ng = normalize(Normal);
    // A thin, two-sided surface (a flag's cloth) seen from behind.
    if (dot(ng, uFogCamPos - FragPos) < 0.0) ng = -ng;
    highp vec2 uv = TexCoords;
    highp vec2 dx = dFdx(uv);
    highp vec2 dy = dFdy(uv);
    vec3 n = ng;
    float rough = uSurfaceDefault.x;
    float metal = uSurfaceDefault.y;
    float occlusion = 1.0;
    float glow = 0.0;
    if (uMaterial.x > 0.5) {
        highp vec3 dp1 = dFdx(FragPos);
        highp vec3 dp2 = dFdy(FragPos);
        highp float det = dx.x * dy.y - dx.y * dy.x;
        if (abs(det) > 1e-16) {
            // The surface's own axes of u and v, in world units per repeat.
            highp vec3 pu = (dp1 * dy.y - dp2 * dx.y) / det;
            highp vec3 pv = (dp2 * dx.x - dp1 * dy.x) / det;
            highp float lu = dot(pu, pu);
            highp float lv = dot(pv, pv);
            if (lu > 1e-10 && lv > 1e-10) {
                highp vec3 toEye = uFogCamPos - FragPos;
                highp float eyeDist = length(toEye);
                vec3 v = toEye / max(eyeDist, 1e-4);
                float nov = max(dot(ng, v), 0.0);
                float deep = uMaterial.y * (1.0 - smoothstep(600.0, 1100.0, eyeDist));
                if (deep > 0.0) {
                    vec3 vt = v - ng * dot(ng, v);
                    highp vec2 reach = -vec2(dot(vt, pu) / lu, dot(vt, pv) / lv)
                                     * (deep / max(nov, 0.3));
                    uv = fioParallax(uv, dx, dy, reach, nov);
                }
                vec3 tn = textureGrad(uNormalMap, uv, dx, dy).xyz * 2.0 - 1.0;
                n = normalize(pu * (tn.x * inversesqrt(lu)) + pv * (tn.y * inversesqrt(lv))
                              + ng * tn.z);
            }
        }
        vec4 mm = textureGrad(uMaterialMap, uv, dx, dy);
        occlusion = mm.r;
        rough = mm.g;
        metal = mm.b;
        glow = (1.0 - mm.a) * uMaterial.z;
    }
    vec4 texColor = textureGrad(texture_diffuse, uv, dx, dy);
    if (texColor.a < 0.1) discard;
    texColor.rgb *= mix(vec3(1.0), model_tint.rgb, model_tint.a);
    vec3 albedo = fioToLinear(texColor.rgb);
    vec3 emission = fioToLinear(emissive_factor) * uEnvParams.z + albedo * glow;
    fioShade(albedo, n, ng, rough, metal, occlusion, emission, FIO_SRC_ALPHA);
}
"""

_LIT_MAIN = """
void main() {
    if (uSurfaceMode == 0) { fioLegacyMain(); return; }
    fioSunBegin();
    vec3 ng = normalize(Normal);
    if (dot(ng, uFogCamPos - FragPos) < 0.0) ng = -ng;
    // An overbright colour is a light source (a glow brush, a neon tube).
    vec3 base = FIO_SRC_COLOUR;
    float over = max(base.r, max(base.g, base.b));
    vec3 albedo = fioToLinear(base / max(over, 1.0));
    vec3 emission = fioToLinear(emissive_factor) * uEnvParams.z;
    if (over > 1.0) emission += albedo * (over - 1.0) * uEnvParams.z;
    fioShade(albedo, ng, ng, uSurfaceDefault.x, uSurfaceDefault.y, 1.0, emission, FIO_SRC_ALPHA);
}
"""

#: The terrain's main in an HD frame. Under a sky it is shaded like any other
#: surface: its layers' colour as albedo, their normal maps, the frame's sun
#: (or, where the sky has none, the fixed sun classic Fio has always lit
#: terrain with), sky light, shadows and haze. Without a sky, or in one of
#: the stylised looks (stepped light, dither), it is the classic terrain,
#: kept as it is through the tone curve.
_TERRAIN_MAIN = """
uniform sampler2D uLayerNormal[4];     // sand, grass, rock, snow
uniform vec4 uLayerMaps;               // which of them are bound
uniform vec4 uLayerRough;
void main() {
    bool sky = uAmbientSky.r + uAmbientSky.g + uAmbientSky.b
             + uSunColor.r + uSunColor.g + uSunColor.b > 0.0;
    if (uSurfaceMode == 0 || !sky || uLightSteps > 0 || uDither > 0) {
        fioLegacyMain();
        if (fioHdr.x > 0.5) {
            FragColor.rgb = fioDisplayToScene(FragColor.rgb);
            FragData = vec4(0.5, 0.5, 1.0, 1.0);
        }
        return;
    }
    fioSunBegin();
    if (uSunColor.r + uSunColor.g + uSunColor.b <= 0.0) {
        fioSunDir = normalize(vec3(0.4, 0.7, 0.3));
        fioSunColor = vec3(3.6, 3.15, 2.45);
        fioSunMapped = 0.0;
    }
FIO_TERRAIN_COLOUR
    float rough = 0.9;
    vec3 n = norm;
    if (uColorMode == 0 && use_textures == 1) {
        vec4 w = layerWeights(FragPos, 1.0 - max(smoothNorm.y, 0.0));
        rough = dot(w, uLayerRough);
        vec3 tn = vec3(0.0);
        if (uLayerMaps.x > 0.5) tn += w.x * (texture(uLayerNormal[0], TexCoords * 1.5).xyz * 2.0 - 1.0);
        else tn.z += w.x;
        if (uLayerMaps.y > 0.5) tn += w.y * (texture(uLayerNormal[1], TexCoords * 1.0).xyz * 2.0 - 1.0);
        else tn.z += w.y;
        if (uLayerMaps.z > 0.5) tn += w.z * (texture(uLayerNormal[2], TexCoords * 0.5).xyz * 2.0 - 1.0);
        else tn.z += w.z;
        if (uLayerMaps.w > 0.5) tn += w.w * (texture(uLayerNormal[3], TexCoords * 0.8).xyz * 2.0 - 1.0);
        else tn.z += w.w;
        // The layers' own axes of u and v across the ground.
        highp vec3 dp1 = dFdx(FragPos);
        highp vec3 dp2 = dFdy(FragPos);
        highp vec2 dx = dFdx(TexCoords);
        highp vec2 dy = dFdy(TexCoords);
        highp float det = dx.x * dy.y - dx.y * dy.x;
        if (abs(det) > 1e-16) {
            highp vec3 pu = (dp1 * dy.y - dp2 * dx.y) / det;
            highp vec3 pv = (dp2 * dx.x - dp1 * dy.x) / det;
            highp float lu = dot(pu, pu);
            highp float lv = dot(pv, pv);
            if (lu > 1e-10 && lv > 1e-10)
                n = normalize(pu * (tn.x * inversesqrt(lu)) + pv * (tn.y * inversesqrt(lv))
                              + norm * max(tn.z, 0.05));
        }
    }
    // The classic terrain lifts its colours' saturation a little; so here.
    float grey = dot(texColor, vec3(0.299, 0.587, 0.114));
    texColor = clamp(mix(vec3(grey), texColor, 1.25), 0.0, 1.0);
    fioShade(fioToLinear(texColor), n, faceNorm, rough, 0.0, 1.0, vec3(0.0), 1.0);
}
"""

_TERRAIN_SPLIT = "    // ---- Lighting -----"


def terrain_source(source):
    """The terrain's fragment shader with the HD path added (see
    :data:`_TERRAIN_MAIN`), or None when it does not have the shape this
    expects. *source* has been through engine.shaders.light_ubo_source."""
    from engine.renderer.hd_high import hdr
    if not source or 'fioLegacyMain' in source or 'terrainPaintMap' not in source:
        return None
    main = _MAIN_RE.search(source)
    lights = re.search(r"Light\s+lights\s*\[\s*(\d+)\s*\]", source)
    split = source.find(_TERRAIN_SPLIT)
    if (main is None or lights is None or split < main.end() or not _OUT_RE.search(source)
            or 'calcPointShadow' not in source or 'fogFactor' not in source):
        return None
    colour = source[main.end():split]          # everything up to the lighting
    head = source[:main.start()]
    # The classic conversion's block goes after the version and precision lines.
    at = 0
    for pattern in (hdr._VERSION_RE, hdr._PRECISION_RE):
        found = pattern.search(head, at)
        if found is not None:
            at = max(at, found.end())
    head = head[:at] + hdr.CLASSIC_GLSL + head[at:]
    body = (head + SURFACE_GLSL
            + _TERRAIN_MAIN.replace('FIO_TERRAIN_COLOUR', colour)
            + "void fioLegacyMain() {" + source[main.end():]
            + _shade_glsl(lights.group(1)))
    return _OUT_RE.sub("layout(location = 0) out vec4 FragColor;", body, count=1)


def _shade_glsl(max_lights):
    return (SURFACE_SHADE_GLSL.replace('FIO_MAX_LIGHTS', str(max_lights))
            .replace('FIO_GRID_CELLS', str(LIGHT_GRID_CELLS))
            .replace('FIO_GRID_LAYERS', str(LIGHT_GRID_LAYERS))
            .replace('FIO_GRID_DEPTH', str(LIGHT_GRID_DEPTH)))


_MAIN_RE = re.compile(r"void\s+main\s*\(\s*(?:void)?\s*\)\s*\{")
_OUT_RE = re.compile(r"^out\s+vec4\s+FragColor\s*;", re.MULTILINE)
_LIGHTS_RE = re.compile(r"uniform\s+Light\s+lights\s*\[\s*(\d+)\s*\]")


def surface_source(source):
    """*source* (a fragment shader built from ``lit.frag`` or
    ``textured.frag``, instanced variants included) with the physically based
    path added, or *source* itself when it does not have the shape this
    rewrite expects -- the program then simply keeps the legacy look."""
    if not source or 'fioLegacyMain' in source:
        return source
    main = _MAIN_RE.search(source)
    lights = _LIGHTS_RE.search(source)
    if (main is None or lights is None or not _OUT_RE.search(source)
            or 'uAmbient' not in source or 'calcPointShadow' not in source
            or 'emissive_factor' not in source or 'in vec3 Normal;' not in source
            or 'fogFactor' not in source):
        return source
    # A model instance's fade, where the variant carries one.
    fade = ' * InstanceAlpha' if 'InstanceAlpha' in source else ''
    if 'texture_diffuse' in source and 'model_tint' in source:
        new_main = _TEXTURED_MAIN
        opacity = 'texColor.a' + (
            ' * alpha' if re.search(r'uniform\s+float\s+alpha\s*;', source) else '')
        colour = ''
    elif 'vInstanceColor' in source:
        new_main, opacity, colour = _LIT_MAIN, 'vInstanceColor.a', 'vInstanceColor.rgb'
    elif 'object_color' in source and re.search(r'uniform\s+float\s+alpha\s*;', source):
        new_main, opacity, colour = _LIT_MAIN, 'alpha', 'object_color'
    else:
        return source
    new_main = (new_main.replace('FIO_SRC_ALPHA', '(' + opacity + fade + ')')
                .replace('FIO_SRC_COLOUR', colour))
    # Above main: the shader's own declarations and helpers, the surface
    # uniforms and helpers. Below it: the shader's own main (now
    # fioLegacyMain) and fioShade -- the two functions with a light loop.
    body = (source[:main.start()] + SURFACE_GLSL + new_main
            + "void fioLegacyMain() {" + source[main.end():]
            + _shade_glsl(lights.group(1)))
    return _OUT_RE.sub("layout(location = 0) out vec4 FragColor;", body, count=1)
