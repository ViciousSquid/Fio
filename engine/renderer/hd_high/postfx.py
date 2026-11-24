"""The HDR frame and how it is finished.

An environment frame (``environment.py``) is not drawn into the view's own
framebuffer but into a floating-point target of the same size:

* colour, gamma-encoded but unclamped (so every shader that has always
  written display colours still can); its alpha, on physically shaded
  surfaces, is the surface's specular reflectance;
* the shading normal and roughness of those surfaces;
* depth and stencil.

Once the opaque world is drawn, :meth:`_postfx_resolve` adds what a forward
pass cannot know: ambient occlusion from the depth buffer, and reflections --
of the frame itself where a reflected ray finds it, of the sky where it does
not. Blended things (sprites, particles, water, glass) are drawn after, onto
the result. :meth:`_postfx_end` then blooms, adds the sun's shafts, tone-maps
(ACES), grades, anti-aliases (FXAA) and puts the picture, and the depth, into
the framebuffer the view asked for.
"""

import math

import glm
import numpy as np
import OpenGL.GL as gl
from engine.renderer.core import gl_state
from ..core.raw_calls import uniform_matrix4
# The raw entry point: PyOpenGL's wrapper has no array handler for the packed
# depth-stencil pixel type, and these allocations pass no pixels anyway.
from OpenGL.raw.GL.VERSION.GL_1_1 import glTexImage2D as _raw_tex_image

from engine import hd_graphics
from engine.renderer.hd_high import surface
from engine.renderer.hd_high.environment import FULLSCREEN_VERT, _Program

#: Bloom's chain starts at a quarter of the frame's size and halves from
#: there. (It used to start at half size with one more level: the half-size
#: level cost as much as all the others together, for the tightest sixth of
#: a glow that is a blur to begin with; BLOOM_GAIN makes up its share.)
BLOOM_LEVELS = 5
BLOOM_GAIN = 6.0 / 5.0

_VIEW_POS_GLSL = """
uniform mat4 uProjection;
uniform mat4 uInvProjection;
uniform sampler2D uDepth;
// A perspective projection's own terms (PostFXMixin._postfx_matrices): the
// view position of a depth sample in a few multiplies. These shaders take
// one for every tap, and a matrix product and a divide each was most of
// what the ambient occlusion pass cost.
uniform highp vec4 uUnprojectA;    // xy: 2 / P00, 2 / P11; zw: (P20 - 1) / P00, (P21 - 1) / P11
uniform highp vec3 uUnprojectB;    // x P22, y P32, z 1 when the projection is a plain perspective
highp vec3 fioViewPos(highp vec2 uv, highp float depth) {
    if (uUnprojectB.z > 0.5) {
        highp float z = -uUnprojectB.y / (depth * 2.0 - 1.0 + uUnprojectB.x);
        return vec3((uv * uUnprojectA.xy + uUnprojectA.zw) * -z, z);
    }
    highp vec4 p = uInvProjection * vec4(uv * 2.0 - 1.0, depth * 2.0 - 1.0, 1.0);
    return p.xyz / p.w;
}
highp vec3 fioViewAt(highp vec2 uv) {
    return fioViewPos(uv, textureLod(uDepth, uv, 0.0).r);
}
float fioDither(vec2 pixel) {
    return fract(52.9829189 * fract(dot(pixel, vec2(0.06711056, 0.00583715))));
}
"""

#: Scalable ambient obscurance (McGuire et al.), at half resolution: how much
#: of the hemisphere above each pixel nearby geometry covers.
AO_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform vec4 uParams;      // radius (world), bias, intensity, radius in pixels at unit distance
uniform vec2 uPixel;       // one full-resolution pixel, in uv
uniform int uTaps;         // samples (the quality tier's), at most 16
uniform vec4 uTurn;        // cos, sin of the turn between two taps; cos, sin of half of it
""" + _VIEW_POS_GLSL + """
void main() {
    highp float depth = textureLod(uDepth, vUV, 0.0).r;
    // Green: how far the pixel is (the blur weighs by it), the sky farthest.
    if (depth >= 0.999999) { FragColor = vec4(1.0, 60000.0, 0.0, 1.0); return; }
    highp vec3 p = fioViewPos(vUV, depth);
    // The surface normal from the depth buffer: the shorter of the two
    // differences on each axis, so an edge does not bend it.
    highp vec3 l = p - fioViewAt(vUV - vec2(uPixel.x, 0.0));
    highp vec3 r = fioViewAt(vUV + vec2(uPixel.x, 0.0)) - p;
    highp vec3 d = p - fioViewAt(vUV - vec2(0.0, uPixel.y));
    highp vec3 u = fioViewAt(vUV + vec2(0.0, uPixel.y)) - p;
    highp vec3 dx = abs(l.z) < abs(r.z) ? l : r;
    highp vec3 dy = abs(d.z) < abs(u.z) ? d : u;
    vec3 n = normalize(cross(dx, dy));
    float radius = uParams.x;
    float reach = min(uParams.w * radius / max(-p.z, 1.0), 96.0);   // in pixels
    if (reach < 1.5) { FragColor = vec4(1.0, min(-p.z, 60000.0), 0.0, 1.0); return; }
    float spin = fioDither(gl_FragCoord.xy) * 6.2831853;
    float total = 0.0;
    int TAPS = clamp(uTaps, 4, 16);
    // The taps wind a spiral, each a fixed turn on from the last: the
    // first direction's sine and cosine, then a rotation a tap (uTurn),
    // instead of a sine and a cosine for every one.
    vec2 dir = vec2(cos(spin), sin(spin));
    dir = vec2(dir.x * uTurn.z - dir.y * uTurn.w, dir.x * uTurn.w + dir.y * uTurn.z);
    for (int i = 0; i < 16; i++) {
        if (i >= TAPS) break;
        float t = (float(i) + 0.5) / float(TAPS);
        vec2 uv = vUV + dir * (t * reach) * uPixel;
        dir = vec2(dir.x * uTurn.x - dir.y * uTurn.y, dir.x * uTurn.y + dir.y * uTurn.x);
        highp vec3 v = fioViewAt(uv) - p;
        float vv = dot(v, v);
        float vn = dot(v, n);
        float f = max(radius * radius - vv, 0.0);
        total += f * f * f * max((vn - uParams.y) / (vv + 0.01), 0.0);
    }
    float r6 = radius * radius * radius * radius * radius * radius;
    float ao = max(0.0, 1.0 - total * uParams.z * 5.0 / (r6 * float(TAPS)));
    // Fade out where the depth buffer is too coarse to trust.
    ao = mix(ao, 1.0, smoothstep(2500.0, 5000.0, -p.z));
    FragColor = vec4(ao, min(-p.z, 60000.0), 0.0, 1.0);
}
"""

#: Depth-aware 3x3 blur of the occlusion: removes the per-pixel rotation's
#: noise without bleeding across edges. The occlusion pass left each texel's
#: distance beside its occlusion, so a tap is one fetch.
AO_BLUR_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform sampler2D uAO;     // r occlusion, g distance
void main() {
    ivec2 here = ivec2(gl_FragCoord.xy);
    ivec2 last = textureSize(uAO, 0) - 1;
    vec2 centre = texelFetch(uAO, here, 0).rg;
    if (centre.g >= 59999.0) { FragColor = vec4(1.0); return; }      // the sky
    float total = centre.r;
    float weight = 1.0;
    for (int y = -1; y <= 1; y++) {
        for (int x = -1; x <= 1; x++) {
            if (x == 0 && y == 0) continue;
            vec2 tap = texelFetch(uAO, clamp(here + ivec2(x, y), ivec2(0), last), 0).rg;
            float w = 1.0 / (1.0 + abs(tap.g - centre.g) * 0.25);
            total += tap.r * w;
            weight += w;
        }
    }
    float ao = total / weight;
    FragColor = vec4(ao, ao, ao, 1.0);
}
"""

#: Occlusion and reflections onto the opaque frame.
RESOLVE_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform sampler2D uColor;
uniform sampler2D uData;
uniform sampler2D uAO;
uniform sampler2D uEnvMap;
uniform mat4 uView;
uniform mat4 uInvView;
uniform vec4 uParams;      // occlusion strength, reflections on, env top mip, colour top mip
uniform vec4 uParams2;     // x the environment map is a probe's whole sphere, y march steps, z gain on the environment map
uniform vec3 uIndoor;      // what a reflection sees where the sky cannot be seen at all
uniform vec3 uFallback;    // what a reflection sees where the frame has no answer and the sky is hidden
""" + surface.ENV_GLSL + _VIEW_POS_GLSL + """
// The frame's colour where the reflected ray meets it (rgb, linear) and a
// confidence (a): a march through the depth buffer with a growing stride,
// then a bisection.
vec4 fioTrace(highp vec3 origin, highp vec3 dir, float rough) {
    highp vec4 clipO = uProjection * vec4(origin, 1.0);
    highp vec4 clipD = uProjection * vec4(dir, 0.0);
    highp float stride = max(2.0, -origin.z * 0.012);
    highp float s = stride * (0.6 + fioDither(gl_FragCoord.xy));
    highp float prev = 0.0;
    int steps = int(uParams2.y);
    for (int i = 0; i < 48; i++) {
        if (i >= steps) break;
        highp float pz = origin.z + dir.z * s;
        if (pz > -0.2) break;
        highp vec4 c = clipO + clipD * s;
        highp vec2 uv = c.xy / c.w * 0.5 + 0.5;
        if (uv.x <= 0.0 || uv.x >= 1.0 || uv.y <= 0.0 || uv.y >= 1.0) break;
        highp float d = textureLod(uDepth, uv, 0.0).r;
        if (d < 0.999999) {
            highp float gap = fioViewPos(uv, d).z - pz;
            if (gap > 0.0 && gap < max(stride * 2.5, 12.0)) {
                highp float lo = prev;
                highp float hi = s;
                for (int j = 0; j < 6; j++) {
                    highp float mid = (lo + hi) * 0.5;
                    highp vec4 mc = clipO + clipD * mid;
                    highp vec2 muv = mc.xy / mc.w * 0.5 + 0.5;
                    if (fioViewAt(muv).z > origin.z + dir.z * mid) hi = mid; else lo = mid;
                }
                highp vec4 hc = clipO + clipD * hi;
                highp vec2 huv = hc.xy / hc.w * 0.5 + 0.5;
                vec2 edge = smoothstep(vec2(0.0), vec2(0.06), huv)
                          * smoothstep(vec2(0.0), vec2(0.06), vec2(1.0) - huv);
                float fade = edge.x * edge.y
                           * (1.0 - smoothstep(uParams2.y - 10.0, uParams2.y, float(i)));
                // A rougher surface reflects a wider cone: a blurrier level
                // of the frame, the farther the hit the blurrier.
                float cone = rough * rough * hi * uProjection[1][1] / max(-origin.z - dir.z * hi, 1.0);
                float lod = clamp(log2(1.0 + cone * 600.0), 0.0, uParams.w);
                return vec4(fioToLinear(textureLod(uColor, huv, lod).rgb), fade);
            }
        }
        prev = s;
        s += stride;
        stride *= 1.16;
    }
    return vec4(0.0);
}
void main() {
    vec4 colour = textureLod(uColor, vUV, 0.0);
    float ao = mix(1.0, textureLod(uAO, vUV, 0.0).r, uParams.x);
    // Only a surface smooth enough to show a picture is marked (alpha) for
    // a reflection from here; a rougher one took the environment's when it
    // was shaded (surface.fioShade). Everything else -- most of most frames
    // -- is the occlusion alone, or nothing at all.
    bool reflects = colour.a < 0.97 && uParams.y > 0.5;
    if (!reflects && ao > 0.996) { FragColor = colour; return; }
    vec3 lin = fioToLinear(colour.rgb);
    // Occlusion is of the sky's light; what the sun lights directly keeps
    // most of its brightness.
    float luma = dot(lin, vec3(0.2126, 0.7152, 0.0722));
    float keep = mix(ao, 1.0, smoothstep(0.5, 2.5, luma) * 0.65);
    if (!reflects) {
        // The colour is gamma-encoded: scaled in place.
        FragColor = vec4(colour.rgb * pow(keep, 1.0 / 2.2), colour.a);
        return;
    }
    lin *= keep;
    highp float depth = textureLod(uDepth, vUV, 0.0).r;
    if (depth >= 0.999999) { FragColor = colour; return; }
    vec4 data = textureLod(uData, vUV, 0.0);
    float rough = data.a;
    highp vec3 p = fioViewPos(vUV, depth);
    vec3 toPoint = normalize(p);
    vec3 n = normalize(mat3(uView) * fioUnpackNormal(data.xy));
    vec3 r = reflect(toPoint, n);
    vec3 world = mat3(uInvView) * r;
    float nov = max(dot(n, -toPoint), 1e-3);
    vec3 brdf = fioEnvBRDF(vec3(colour.a), rough, nov);
    vec3 seen = mix(uFallback,
                    textureLod(uEnvMap, fioEnvUV(world), rough * uParams.z).rgb * uParams2.z,
                    uParams2.x > 0.5 ? 1.0 : smoothstep(-0.05, 0.45, world.y));
    // data.z: how much of the sky's light reaches the surface at all.
    if (uParams2.x < 0.5) seen = mix(uIndoor, seen, data.z);
    if (max(brdf.r, max(brdf.g, brdf.b)) > 0.006) {
        vec4 hit = fioTrace(p + n * max(0.25, -p.z * 0.002), r, rough);
        seen = mix(seen, min(hit.rgb, vec3(6.0)),
                   hit.a * (1.0 - smoothstep(0.22, 0.5, rough)));
    }
    lin += seen * brdf * mix(1.0, ao, 0.7);
    FragColor = vec4(fioEncode(lin), colour.a);
}
"""

#: The bright part of the frame at a quarter of its size, then each level
#: at half the one before (13 taps, Jimenez).
BLOOM_DOWN_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform sampler2D uSource;
uniform vec2 uTexel;       // one source texel, in uv
uniform vec4 uParams;      // x 1 for the first level (decode, threshold), y threshold, z knee
""" + surface.ENV_GLSL + """
vec3 fioTap(vec2 uv) {
    vec3 c = textureLod(uSource, uv, 0.0).rgb;
    if (uParams.x > 0.5) {
        c = fioToLinear(c);
        float brightness = max(c.r, max(c.g, c.b));
        float soft = clamp(brightness - uParams.y + uParams.z, 0.0, 2.0 * uParams.z);
        soft = soft * soft / (4.0 * uParams.z + 1e-4);
        c *= max(soft, brightness - uParams.y) / max(brightness, 1e-4);
        // A single very bright pixel must not flicker through the chain.
        c /= 1.0 + dot(c, vec3(0.2126, 0.7152, 0.0722)) * 0.04;
    }
    return c;
}
void main() {
    vec2 t = uTexel;
    if (uParams.x > 0.5) {
        // The first level, a quarter of the frame's size: each pixel is the
        // mean of the sixteen texels under it, in four filtered taps.
        vec3 sum = fioTap(vUV + t) + fioTap(vUV - t)
                 + fioTap(vUV + vec2(t.x, -t.y)) + fioTap(vUV + vec2(-t.x, t.y));
        FragColor = vec4(max(sum * 0.25, vec3(0.0)), 1.0);
        return;
    }
    vec3 a = fioTap(vUV + t * vec2(-2.0, 2.0));
    vec3 b = fioTap(vUV + t * vec2(0.0, 2.0));
    vec3 c = fioTap(vUV + t * vec2(2.0, 2.0));
    vec3 d = fioTap(vUV + t * vec2(-2.0, 0.0));
    vec3 e = fioTap(vUV);
    vec3 f = fioTap(vUV + t * vec2(2.0, 0.0));
    vec3 g = fioTap(vUV + t * vec2(-2.0, -2.0));
    vec3 h = fioTap(vUV + t * vec2(0.0, -2.0));
    vec3 i = fioTap(vUV + t * vec2(2.0, -2.0));
    vec3 j = fioTap(vUV + t * vec2(-1.0, 1.0));
    vec3 k = fioTap(vUV + t * vec2(1.0, 1.0));
    vec3 l = fioTap(vUV + t * vec2(-1.0, -1.0));
    vec3 m = fioTap(vUV + t * vec2(1.0, -1.0));
    vec3 sum = e * 0.125 + (a + c + g + i) * 0.03125 + (b + d + f + h) * 0.0625
             + (j + k + l + m) * 0.125;
    FragColor = vec4(max(sum, vec3(0.0)), 1.0);
}
"""

BLOOM_UP_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform sampler2D uSource;
uniform vec2 uTexel;
void main() {
    vec2 t = uTexel;
    vec3 sum = textureLod(uSource, vUV, 0.0).rgb * 4.0;
    sum += (textureLod(uSource, vUV + vec2(t.x, 0.0), 0.0).rgb
          + textureLod(uSource, vUV - vec2(t.x, 0.0), 0.0).rgb
          + textureLod(uSource, vUV + vec2(0.0, t.y), 0.0).rgb
          + textureLod(uSource, vUV - vec2(0.0, t.y), 0.0).rgb) * 2.0;
    sum += textureLod(uSource, vUV + t, 0.0).rgb + textureLod(uSource, vUV - t, 0.0).rgb
         + textureLod(uSource, vUV + vec2(t.x, -t.y), 0.0).rgb
         + textureLod(uSource, vUV + vec2(-t.x, t.y), 0.0).rgb;
    FragColor = vec4(sum / 16.0, 1.0);
}
"""

#: Light shafts: how much open sky lies between each pixel and the sun on
#: screen (a radial march through the depth buffer, at half size).
SHAFTS_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform sampler2D uDepth;
uniform vec4 uSun;         // sun on screen (uv), strength, aspect
float fioDither(vec2 pixel) {
    return fract(52.9829189 * fract(dot(pixel, vec2(0.06711056, 0.00583715))));
}
void main() {
    vec2 toSun = uSun.xy - vUV;
    const int STEPS = 28;
    vec2 delta = toSun / float(STEPS);
    vec2 uv = vUV + delta * fioDither(gl_FragCoord.xy);
    // The weight of a step, decay * exp(-9 |off|^2) with off moving a fixed
    // step a time, is a Gaussian along a line: kept by two multiplies a
    // step (w *= r, r *= k) instead of an exp each.
    vec2 off = (uv - uSun.xy) * vec2(uSun.w, 1.0);
    vec2 step_ = delta * vec2(uSun.w, 1.0);
    float b = dot(off, step_);
    float c = dot(step_, step_);
    float w = exp(-9.0 * dot(off, off));
    float r = exp(-9.0 * (2.0 * b + c)) * 0.955;
    float k = exp(-18.0 * c);
    float light = 0.0;
    for (int i = 0; i < STEPS; i++) {
        float open = step(0.999999, textureLod(uDepth, clamp(uv, 0.0, 1.0), 0.0).r);
        light += open * w;
        w *= r;
        r *= k;
        uv += delta;
    }
    vec2 here = (vUV - uSun.xy) * vec2(uSun.w, 1.0);
    float glow = light / float(STEPS) * uSun.z * exp(-dot(here, here) * 1.6);
    FragColor = vec4(glow, glow, glow, 1.0);
}
"""

#: Bloom, shafts, exposure, tone mapping, grade, vignette and grain;
#: display-encoded, with luma in alpha for the anti-aliasing pass.
COMPOSE_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform sampler2D uColor;
uniform sampler2D uBloom;
uniform sampler2D uShafts;
uniform sampler2D uNoise;
uniform vec4 uParams;      // exposure, bloom, saturation, contrast
uniform vec4 uParams2;     // vignette, grain, shafts on, time
uniform vec3 uTint;
uniform vec3 uSunColor;
uniform float uOpaque;     // 1: the last pass (no FXAA after): alpha 1, not luma
""" + surface.ENV_GLSL + """
// Extended Reinhard: straight through the shadows and mid-tones, rolling
// off toward white (hd_graphics.tone; hdr.py inverts it for the classic
// programs).
vec3 fioTone(vec3 x) {
    return clamp(x * (1.0 + x / 16.0000) / (1.0 + x), 0.0, 1.0);
}
void main() {
    vec3 colour = fioToLinear(textureLod(uColor, vUV, 0.0).rgb);
    if (uParams.y > 0.0) colour += textureLod(uBloom, vUV, 0.0).rgb * uParams.y;
    if (uParams2.z > 0.5) colour += uSunColor * textureLod(uShafts, vUV, 0.0).r;
    colour *= uTint * uParams.x;
    colour = fioTone(colour);
    // Grade on the display-referred image: contrast about mid grey, then
    // saturation about luma.
    colour = pow(colour, vec3(1.0 / 2.2));
    colour = clamp((colour - 0.5) * uParams.w + 0.5, 0.0, 1.0);
    float luma = dot(colour, vec3(0.299, 0.587, 0.114));
    colour = clamp(mix(vec3(luma), colour, uParams.z), 0.0, 1.0);
    vec2 fromCentre = vUV - 0.5;
    colour *= 1.0 - uParams2.x * smoothstep(0.25, 1.1, dot(fromCentre, fromCentre) * 2.4);
    float grain = textureLod(uNoise, gl_FragCoord.xy / 256.0 + vec2(uParams2.w * 7.31, uParams2.w * 3.17), 0.0).r - 0.5;
    colour += grain * uParams2.y * (1.0 - luma * 0.6);
    colour = clamp(colour, 0.0, 1.0);
    FragColor = vec4(colour, uOpaque > 0.5 ? 1.0 : dot(colour, vec3(0.299, 0.587, 0.114)));
}
"""

#: FXAA (Lottes), the compact form: blur along the edge the local luma
#: gradient finds.
FXAA_FRAG = """#version 330 core
precision highp float;
layout(location = 0) out vec4 FragColor;
in vec2 vUV;
uniform sampler2D uSource;
uniform vec2 uTexel;
uniform int uEnabled;
void main() {
    vec4 centre = textureLod(uSource, vUV, 0.0);
    if (uEnabled == 0) { FragColor = vec4(centre.rgb, 1.0); return; }
    float lumaNW = textureLod(uSource, vUV + vec2(-1.0, -1.0) * uTexel, 0.0).a;
    float lumaNE = textureLod(uSource, vUV + vec2(1.0, -1.0) * uTexel, 0.0).a;
    float lumaSW = textureLod(uSource, vUV + vec2(-1.0, 1.0) * uTexel, 0.0).a;
    float lumaSE = textureLod(uSource, vUV + vec2(1.0, 1.0) * uTexel, 0.0).a;
    float lumaM = centre.a;
    float lumaMin = min(lumaM, min(min(lumaNW, lumaNE), min(lumaSW, lumaSE)));
    float lumaMax = max(lumaM, max(max(lumaNW, lumaNE), max(lumaSW, lumaSE)));
    if (lumaMax - lumaMin < max(0.0312, lumaMax * 0.125)) {
        FragColor = vec4(centre.rgb, 1.0);
        return;
    }
    vec2 dir = vec2(-((lumaNW + lumaNE) - (lumaSW + lumaSE)),
                    (lumaNW + lumaSW) - (lumaNE + lumaSE));
    float reduce = max((lumaNW + lumaNE + lumaSW + lumaSE) * 0.03125, 0.0078125);
    float scale = 1.0 / (min(abs(dir.x), abs(dir.y)) + reduce);
    dir = clamp(dir * scale, vec2(-8.0), vec2(8.0)) * uTexel;
    vec4 a = 0.5 * (textureLod(uSource, vUV + dir * (1.0 / 3.0 - 0.5), 0.0)
                  + textureLod(uSource, vUV + dir * (2.0 / 3.0 - 0.5), 0.0));
    vec4 b = a * 0.5 + 0.25 * (textureLod(uSource, vUV - dir * 0.5, 0.0)
                             + textureLod(uSource, vUV + dir * 0.5, 0.0));
    FragColor = vec4((b.a < lumaMin || b.a > lumaMax) ? a.rgb : b.rgb, 1.0);
}
"""


def query_result(query):
    """A query's 64-bit result (waits for it). PyOpenGL's own wrapper has no
    array type for it, so it is read into a ctypes value."""
    import ctypes
    value = ctypes.c_uint64(0)
    gl.glGetQueryObjectui64v(int(query), gl.GL_QUERY_RESULT, ctypes.byref(value))
    return int(value.value)


def _texture(internal, width, height, pixel_format, pixel_type, *, linear=True,
             mipmaps=False):
    texture = int(gl.glGenTextures(1))
    gl_state.glBindTexture(gl.GL_TEXTURE_2D, texture)
    _raw_tex_image(gl.GL_TEXTURE_2D, 0, internal, width, height, 0,
                   pixel_format, pixel_type, None)
    filtering = gl.GL_LINEAR if linear else gl.GL_NEAREST
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER,
                       gl.GL_LINEAR_MIPMAP_LINEAR if mipmaps else filtering)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, filtering)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_S, gl.GL_CLAMP_TO_EDGE)
    gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_WRAP_T, gl.GL_CLAMP_TO_EDGE)
    if mipmaps:
        gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
    return texture


def _framebuffer(*colours, depth_stencil=0):
    framebuffer = int(gl.glGenFramebuffers(1))
    gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
    for index, texture in enumerate(colours):
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0 + index,
                                  gl.GL_TEXTURE_2D, texture, 0)
    if depth_stencil:
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_DEPTH_STENCIL_ATTACHMENT,
                                  gl.GL_TEXTURE_2D, depth_stencil, 0)
    gl.glDrawBuffers(len(colours), [gl.GL_COLOR_ATTACHMENT0 + i for i in range(len(colours))])
    gl.glReadBuffer(gl.GL_COLOR_ATTACHMENT0)
    status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
    if status != gl.GL_FRAMEBUFFER_COMPLETE:
        raise RuntimeError("HDR framebuffer incomplete: 0x%x" % status)
    return framebuffer


class PostFXMixin:
    """HDHighRenderer's HDR target and post-processing chain."""

    #: Where the benchmark's timestamps are taken in an HD frame, in order.
    HD_MARKS = ('begin', 'sun shadows', 'scene', 'ambient occlusion', 'reflections',
                'blended', 'bloom', 'sun shafts', 'compose')

    def hd_timing(self, on=True):
        """Take a GL timestamp at each of :data:`HD_MARKS` in HD frames
        (tools/bench_hd.py). Off, which is how it starts, a mark is one
        attribute test."""
        if on and self._hd_marks is None:
            self._hd_marks = {}
            self._hd_mark_frames = []
        elif not on:
            self._hd_marks = None

    def _hd_mark(self, name):
        marks = self._hd_marks
        if marks is None:
            return
        query = int(np.asarray(gl.glGenQueries(1)).ravel()[0])
        gl.glQueryCounter(query, gl.GL_TIMESTAMP)
        marks[name] = query

    def hd_timing_collect(self):
        """Finished frames' pass times: a list of ``{pass: milliseconds}``
        (each mark's time since the mark before it). Waits for the GPU."""
        frames, self._hd_mark_frames = self._hd_mark_frames, []
        out = []
        for marks in frames:
            stamps = {}
            for name, query in marks.items():
                stamps[name] = query_result(query)
                gl.glDeleteQueries(1, [query])
            previous = stamps.get('begin')
            passes = {}
            for name in self.HD_MARKS[1:]:
                if name in stamps and previous is not None:
                    passes[name] = (stamps[name] - previous) * 1e-6
                    previous = stamps[name]
            if 'begin' in stamps and previous is not None:
                passes['total'] = (previous - stamps['begin']) * 1e-6
            out.append(passes)
        return out

    def _init_postfx(self):
        self._hd_marks = None
        self._hd_mark_frames = []
        self._postfx_size = (0, 0)
        self._postfx_textures = {}
        self._postfx_fbos = {}
        self._postfx_bloom = []          # [(texture, fbo, width, height)]
        self._postfx_target = None       # (fbo, viewport, scissor on)
        self._postfx_resolved = False
        #: (framebuffer, size, viewport) -> whether the depth blit works there.
        self._postfx_blit_works = {}

    def _postfx_program(self, name, fragment):
        return self._env_program(name, FULLSCREEN_VERT, fragment)

    def _postfx_reflects(self):
        return bool(self.environment.reflections)

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------
    def _postfx_release_targets(self):
        textures = list(self._postfx_textures.values()) + [b[0] for b in self._postfx_bloom]
        fbos = list(self._postfx_fbos.values()) + [b[1] for b in self._postfx_bloom]
        if textures:
            gl_state.glDeleteTextures(textures)
        if fbos:
            gl.glDeleteFramebuffers(len(fbos), fbos)
        self._postfx_textures = {}
        self._postfx_fbos = {}
        self._postfx_bloom = []
        self._postfx_size = (0, 0)

    def _postfx_allocate(self, width, height):
        self._postfx_release_targets()
        tex, fbo = self._postfx_textures, self._postfx_fbos
        tex['colour'] = _texture(gl.GL_RGBA16F, width, height, gl.GL_RGBA, gl.GL_FLOAT,
                                 mipmaps=True)
        tex['data'] = _texture(gl.GL_RGBA16F, width, height, gl.GL_RGBA, gl.GL_FLOAT,
                               linear=False)
        tex['depth'] = _texture(gl.GL_DEPTH24_STENCIL8, width, height,
                                gl.GL_DEPTH_STENCIL, gl.GL_UNSIGNED_INT_24_8, linear=False)
        fbo['scene'] = _framebuffer(tex['colour'], tex['data'], depth_stencil=tex['depth'])
        # The resolve pass draws into this one, and the two then change
        # places (no copy back): the same kind of texture as the colour.
        tex['resolved'] = _texture(gl.GL_RGBA16F, width, height, gl.GL_RGBA, gl.GL_FLOAT,
                                   mipmaps=True)
        fbo['resolved'] = _framebuffer(tex['resolved'])
        half_w, half_h = max(1, width // 2), max(1, height // 2)
        # Occlusion with each texel's distance beside it, for the blur.
        tex['ao'] = _texture(gl.GL_RG16F, half_w, half_h, gl.GL_RG, gl.GL_FLOAT, linear=False)
        fbo['ao'] = _framebuffer(tex['ao'])
        for name in ('ao_blur', 'shafts'):
            tex[name] = _texture(gl.GL_R8, half_w, half_h, gl.GL_RED, gl.GL_UNSIGNED_BYTE)
            fbo[name] = _framebuffer(tex[name])
        tex['ldr'] = _texture(gl.GL_RGBA8, width, height, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        fbo['ldr'] = _framebuffer(tex['ldr'])
        w, h = width, height
        for level in range(BLOOM_LEVELS):
            shrink = 4 if level == 0 else 2
            w, h = max(1, w // shrink), max(1, h // shrink)
            texture = _texture(gl.GL_R11F_G11F_B10F, w, h, gl.GL_RGB, gl.GL_FLOAT)
            self._postfx_bloom.append((texture, _framebuffer(texture), w, h))
            if w == 1 and h == 1:
                break
        self._postfx_size = (width, height)

    # ------------------------------------------------------------------
    # The frame
    # ------------------------------------------------------------------
    def _postfx_begin(self):
        """Send what is drawn from here on to the HDR target, the size of
        the current viewport."""
        viewport = tuple(int(v) for v in gl.glGetIntegerv(gl.GL_VIEWPORT))
        width, height = viewport[2], viewport[3]
        if width < 8 or height < 8:
            return False
        target = int(gl.glGetIntegerv(gl.GL_DRAW_FRAMEBUFFER_BINDING))
        scissor = bool(gl.glIsEnabled(gl.GL_SCISSOR_TEST))
        if self._postfx_size != (width, height):
            self._postfx_allocate(width, height)
        self._postfx_target = (target, viewport, scissor)
        self._postfx_resolved = False
        if self._hd_marks is not None:
            self._hd_marks = {}
            self._hd_mark('begin')
        gl_state.glDisable(gl.GL_SCISSOR_TEST)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self._postfx_fbos['scene'])
        gl.glViewport(0, 0, width, height)
        return True

    def _postfx_pass_state(self):
        gl_state.glDisable(gl.GL_DEPTH_TEST)
        gl_state.glDepthMask(gl.GL_FALSE)
        gl_state.glDisable(gl.GL_BLEND)
        gl_state.glDisable(gl.GL_CULL_FACE)
        gl_state.glDisable(gl.GL_STENCIL_TEST)
        gl_state.glPolygonMode(gl.GL_FRONT_AND_BACK, gl.GL_FILL)
        gl.glColorMask(gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE, gl.GL_TRUE)

    @staticmethod
    def _postfx_bind(program, name, unit, texture):
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + unit)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, texture)
        gl_state.glUniform1i(program[name], unit)

    def _postfx_matrices(self, program, projection):
        # Held in a local: value_ptr points into the matrix it is given.
        inverse = glm.inverse(projection)
        uniform_matrix4(program['uProjection'], glm.value_ptr(projection))
        uniform_matrix4(program['uInvProjection'], glm.value_ptr(inverse))
        # A plain perspective (no shear between x, y and w, which a portal's
        # oblique plane or an orthographic view would have): its terms.
        p = projection
        plain = (abs(p[2][3] + 1.0) < 1e-6 and abs(p[3][3]) < 1e-9
                 and abs(p[0][0]) > 1e-9 and abs(p[1][1]) > 1e-9
                 and max(abs(p[0][1]), abs(p[1][0]), abs(p[3][0]), abs(p[3][1]),
                         abs(p[0][2]), abs(p[1][2]), abs(p[0][3]), abs(p[1][3])) < 1e-9)
        if plain:
            gl_state.glUniform4f(program['uUnprojectA'], 2.0 / p[0][0], 2.0 / p[1][1],
                           (p[2][0] - 1.0) / p[0][0], (p[2][1] - 1.0) / p[1][1])
            gl_state.glUniform3f(program['uUnprojectB'], p[2][2], p[3][2], 1.0)
        else:
            gl_state.glUniform3f(program['uUnprojectB'], 0.0, 0.0, 0.0)

    def _postfx_resolve(self, projection, view):
        """Ambient occlusion and reflections onto the opaque frame, before
        anything blended is drawn over it."""
        if not self._env_active:
            return
        if self._portal_scene_pass:
            self._env_phase = 2         # the portal view's own blended things
            return
        if self._postfx_resolved:
            return
        self._postfx_resolved = True
        self._env_phase = 2
        self._hd_mark('scene')
        env = self.environment
        occlude = env.ambient_occlusion > 0.0
        reflect = self._postfx_reflects()
        if not occlude and not reflect:
            return
        tex, fbo = self._postfx_textures, self._postfx_fbos
        width, height = self._postfx_size
        half_w, half_h = max(1, width // 2), max(1, height // 2)
        cull = bool(gl.glIsEnabled(gl.GL_CULL_FACE))
        stencil = bool(gl.glIsEnabled(gl.GL_STENCIL_TEST))
        self._postfx_pass_state()
        if occlude:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo['ao'])
            gl.glViewport(0, 0, half_w, half_h)
            program = self._postfx_program('ao', AO_FRAG)
            gl_state.glUseProgram(program.id)
            self._postfx_matrices(program, projection)
            self._postfx_bind(program, 'uDepth', 0, tex['depth'])
            gl_state.glUniform4f(program['uParams'], env.ao_radius, env.ao_radius * 0.04,
                           1.0, projection[1][1] * height * 0.5)
            gl_state.glUniform2f(program['uPixel'], 1.0 / width, 1.0 / height)
            taps = max(4, min(16, int(env.ao_taps)))
            gl_state.glUniform1i(program['uTaps'], taps)
            turn = 2.0 * math.pi * 5.0 / taps
            gl_state.glUniform4f(program['uTurn'], math.cos(turn), math.sin(turn),
                           math.cos(turn * 0.5), math.sin(turn * 0.5))
            self._env_fullscreen()
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo['ao_blur'])
            program = self._postfx_program('ao_blur', AO_BLUR_FRAG)
            gl_state.glUseProgram(program.id)
            self._postfx_bind(program, 'uAO', 1, tex['ao'])
            self._env_fullscreen()
        self._hd_mark('ambient occlusion')
        if reflect:
            # Blurrier levels of the frame, for rough reflections.
            gl_state.glActiveTexture(gl.GL_TEXTURE0)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, tex['colour'])
            gl.glGenerateMipmap(gl.GL_TEXTURE_2D)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo['resolved'])
        gl.glViewport(0, 0, width, height)
        program = self._postfx_program('resolve', RESOLVE_FRAG)
        gl_state.glUseProgram(program.id)
        self._postfx_matrices(program, projection)
        inverse_view = glm.inverse(view)
        uniform_matrix4(program['uView'], glm.value_ptr(view))
        uniform_matrix4(program['uInvView'], glm.value_ptr(inverse_view))
        self._postfx_bind(program, 'uDepth', 0, tex['depth'])
        self._postfx_bind(program, 'uColor', 1, tex['colour'])
        self._postfx_bind(program, 'uData', 2, tex['data'])
        self._postfx_bind(program, 'uAO', 3, tex['ao_blur'])
        self._postfx_bind(program, 'uEnvMap', surface.ENV_MAP_UNIT,
                          self._env_reflection_texture())
        levels = math.floor(math.log2(max(width, height)))
        probe = self._env_probe_reflections
        gl_state.glUniform4f(program['uParams'], env.ambient_occlusion if occlude else 0.0,
                       1.0 if reflect else 0.0,
                       math.log2(512.0) - (3.0 if probe else 2.0), min(6.0, float(levels)))
        gl_state.glUniform4f(program['uParams2'], 1.0 if probe else 0.0,
                       float(max(8, min(48, int(env.ssr_steps)))),
                       self._env_reflection_gain(), 0.0)
        gl_state.glUniform3f(program['uIndoor'], *self._env_indoor_colour)
        gl_state.glUniform3f(program['uFallback'], *self._env_reflect_fallback())
        self._env_fullscreen()
        # The result becomes the scene target's colour (whose depth the
        # blended passes test) and the old colour the next frame's result:
        # the two textures change places, and nothing is copied.
        tex['colour'], tex['resolved'] = tex['resolved'], tex['colour']
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, tex['resolved'], 0)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo['scene'])
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, tex['colour'], 0)
        self._postfx_restore_units()
        gl_state.glUseProgram(0)
        self._current_shader = None
        gl_state.glEnable(gl.GL_DEPTH_TEST)
        gl_state.glDepthFunc(gl.GL_LESS)
        gl_state.glDepthMask(gl.GL_TRUE)
        if cull:
            gl_state.glEnable(gl.GL_CULL_FACE)
        if stencil:
            gl_state.glEnable(gl.GL_STENCIL_TEST)
        self._hd_mark('reflections')

    def _postfx_restore_units(self):
        """Put back the units a full-screen pass borrowed: the passes that
        follow expect their own textures on 0..3 to be theirs to bind, and
        the environment's on their units."""
        for unit in (3, 2, 1):
            gl_state.glActiveTexture(gl.GL_TEXTURE0 + unit)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, 0)
        gl_state.glActiveTexture(gl.GL_TEXTURE0 + surface.ENV_MAP_UNIT)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, self._env_reflection_texture())
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, 0)
        # Units 2 and 3 are also where the glass and water passes keep their
        # copies of the frame; they bind them again when they next draw.
        self.shadow_units_changed()

    def _postfx_sun_on_screen(self, projection, view):
        """The sun's position in the frame (uv) and how strongly its shafts
        show, or None when it is behind the view."""
        direction = glm.vec3(*self._env_sun_dir)
        in_view = glm.mat3(view) * direction
        if in_view.z > -0.05:
            return None
        clip = projection * glm.vec4(in_view, 0.0)
        if abs(clip.w) < 1e-6:
            return None
        uv = (clip.x / clip.w * 0.5 + 0.5, clip.y / clip.w * 0.5 + 0.5)
        # Fades as the sun leaves the frame.
        away = max(abs(uv[0] - 0.5), abs(uv[1] - 0.5))
        strength = max(0.0, 1.0 - max(0.0, away - 0.5) / 0.6)
        return uv, strength

    def _postfx_end(self, projection, view):
        """Finish the frame into the framebuffer and viewport it was asked
        for."""
        if not self._env_active:
            return
        if self._portal_scene_pass:
            self._env_end_portal()
            return
        self._hd_mark('blended')
        self._env_active = False
        self._env_phase = 0
        self._hdr_set(False)
        if self._env_clear_colour is not None:
            gl.glClearColor(*self._env_clear_colour)
            self._env_clear_colour = None
        env = self.environment
        exposure = hd_graphics.final_exposure(self._env_base_exposure,
                                              env.exposure_compensation)
        tex, fbo = self._postfx_textures, self._postfx_fbos
        width, height = self._postfx_size
        target, viewport, scissor = self._postfx_target
        self._postfx_pass_state()
        bloom = env.bloom > 0.0
        if bloom:
            self._postfx_bloom_chain(tex['colour'], width, height, exposure)
        self._hd_mark('bloom')
        self._postfx_finish(projection, view, exposure, bloom, target, viewport, scissor)

    def _postfx_bloom_chain(self, source, width, height, exposure):
        """Bloom: down the chain, then back up, each level added to the next."""
        env = self.environment
        source_w, source_h = width, height
        down = self._postfx_program('bloom_down', BLOOM_DOWN_FRAG)
        gl_state.glUseProgram(down.id)
        gl_state.glActiveTexture(gl.GL_TEXTURE0)
        gl_state.glBindTexture(gl.GL_TEXTURE_2D, source)
        gl_state.glUniform1i(down['uSource'], 0)
        for index, (texture, framebuffer, w, h) in enumerate(self._postfx_bloom):
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, framebuffer)
            gl.glViewport(0, 0, w, h)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, source)
            gl_state.glUniform2f(down['uTexel'], 1.0 / source_w, 1.0 / source_h)
            # The threshold is of the exposed picture.
            threshold = env.bloom_threshold / max(exposure, 1e-6)
            gl_state.glUniform4f(down['uParams'], 1.0 if index == 0 else 0.0,
                           threshold, max(threshold * 0.5, 1e-3), 0.0)
            self._env_fullscreen()
            source, source_w, source_h = texture, w, h
        up = self._postfx_program('bloom_up', BLOOM_UP_FRAG)
        gl_state.glUseProgram(up.id)
        gl_state.glUniform1i(up['uSource'], 0)
        gl_state.glEnable(gl.GL_BLEND)
        gl_state.glBlendFunc(gl.GL_ONE, gl.GL_ONE)
        for index in range(len(self._postfx_bloom) - 1, 0, -1):
            texture, _framebuffer_, w, h = self._postfx_bloom[index]
            _target_tex, target_fbo, target_w, target_h = self._postfx_bloom[index - 1]
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target_fbo)
            gl.glViewport(0, 0, target_w, target_h)
            gl_state.glBindTexture(gl.GL_TEXTURE_2D, texture)
            gl_state.glUniform2f(up['uTexel'], 1.0 / w, 1.0 / h)
            self._env_fullscreen()
        gl_state.glDisable(gl.GL_BLEND)
        gl_state.glBlendFunc(gl.GL_SRC_ALPHA, gl.GL_ONE_MINUS_SRC_ALPHA)

    def _postfx_finish(self, projection, view, exposure, bloom, target, viewport, scissor):
        """Sun shafts, tone mapping and grade, FXAA, into the view's own
        framebuffer, and the frame's depth after it."""
        env = self.environment
        tex, fbo = self._postfx_textures, self._postfx_fbos
        width, height = self._postfx_size
        # The sun's shafts.
        sun = self._postfx_sun_on_screen(projection, view) if env.sun_shafts > 0.0 else None
        shafts = sun is not None and sun[1] > 0.0
        if shafts:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo['shafts'])
            gl.glViewport(0, 0, max(1, width // 2), max(1, height // 2))
            program = self._postfx_program('shafts', SHAFTS_FRAG)
            gl_state.glUseProgram(program.id)
            self._postfx_bind(program, 'uDepth', 0, tex['depth'])
            gl_state.glUniform4f(program['uSun'], sun[0][0], sun[0][1],
                           env.sun_shafts * sun[1], width / float(height))
            self._env_fullscreen()
        self._hd_mark('sun shafts')
        # Tone map and grade: into the display target for FXAA to read, or,
        # without FXAA, straight into the view's framebuffer (one full-screen
        # pass fewer).
        fxaa = bool(env.fxaa)
        if fxaa:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo['ldr'])
            gl.glViewport(0, 0, width, height)
        else:
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
            gl.glViewport(*viewport)
        program = self._postfx_program('compose', COMPOSE_FRAG)
        gl_state.glUseProgram(program.id)
        self._postfx_bind(program, 'uColor', 0, tex['colour'])
        self._postfx_bind(program, 'uBloom', 1, self._postfx_bloom[0][0])
        self._postfx_bind(program, 'uShafts', 2, tex['shafts'])
        self._postfx_bind(program, 'uNoise', surface.NOISE_UNIT, self._env_noise())
        gl_state.glUniform4f(program['uParams'], exposure,
                       env.bloom * BLOOM_GAIN if bloom else 0.0, env.saturation, env.contrast)
        gl_state.glUniform1f(program['uOpaque'], 0.0 if fxaa else 1.0)
        gl_state.glUniform4f(program['uParams2'], env.vignette, env.grain, 1.0 if shafts else 0.0,
                       self._env_time % 64.0)
        gl_state.glUniform3f(program['uTint'], *env.tint)
        gl_state.glUniform3f(program['uSunColor'], *self._env_sun_rgb)
        self._env_fullscreen()
        if fxaa:
            # Anti-alias into the framebuffer the view asked for.
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
            gl.glViewport(*viewport)
            program = self._postfx_program('fxaa', FXAA_FRAG)
            gl_state.glUseProgram(program.id)
            self._postfx_bind(program, 'uSource', 0, tex['ldr'])
            gl_state.glUniform2f(program['uTexel'], 1.0 / width, 1.0 / height)
            gl_state.glUniform1i(program['uEnabled'], 1)
            self._env_fullscreen()
        # What the view draws next (its own overlays) is depth-tested
        # against the world: give its framebuffer the frame's depth.
        self._postfx_blit_depth(fbo['scene'], target, width, height, viewport)
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, target)
        self._postfx_restore_units()
        gl_state.glUseProgram(0)
        self._current_shader = None
        if scissor:
            gl_state.glEnable(gl.GL_SCISSOR_TEST)
        gl_state.glDepthMask(gl.GL_TRUE)
        gl_state.glEnable(gl.GL_DEPTH_TEST)
        gl_state.glDepthFunc(gl.GL_LESS)
        if self._hd_marks is not None:
            self._hd_mark('compose')
            self._hd_mark_frames.append(self._hd_marks)
            self._hd_marks = {}

    def _postfx_blit_depth(self, source, target, width, height, viewport):
        """Copy the frame's depth into the view's framebuffer. Whether that
        framebuffer can take it (a depth buffer of a matching format) is
        found out once for each framebuffer and size, by the blit's own
        error, not by draining the error queue every frame (a glGetError a
        frame is a pipeline sync on threaded drivers)."""
        key = (int(target), width, height, tuple(viewport))
        works = self._postfx_blit_works.get(key)
        if works is False:
            return
        if works is None:
            while gl.glGetError() != gl.GL_NO_ERROR:
                pass
        try:
            gl.glBindFramebuffer(gl.GL_READ_FRAMEBUFFER, source)
            gl.glBindFramebuffer(gl.GL_DRAW_FRAMEBUFFER, target)
            gl.glBlitFramebuffer(0, 0, width, height, viewport[0], viewport[1],
                                 viewport[0] + width, viewport[1] + height,
                                 gl.GL_DEPTH_BUFFER_BIT, gl.GL_NEAREST)
            failed = False
        except Exception:
            failed = True
        if works is None:
            while gl.glGetError() != gl.GL_NO_ERROR:
                failed = True
            if len(self._postfx_blit_works) >= 16:
                self._postfx_blit_works.clear()
            self._postfx_blit_works[key] = not failed

    def _cleanup_postfx(self):
        self._postfx_release_targets()
        self._postfx_target = None
