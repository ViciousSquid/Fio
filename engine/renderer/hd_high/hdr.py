"""Classic programs inside an HD frame.

An HD frame (``postfx.py``) is tone-mapped at the end. The physically shaded
surfaces (``surface.py``) write scene light, which is what a tone curve is
for. Everything else -- sprites, particles, effects, water, glass, the sky
panorama, fog volumes, editor overlays -- is drawn by programs that have
always written the colour meant for the display; pushed through the same
curve they would come out darker and greyer than in classic Fio.

So in an HD renderer each of those programs gets a new ``main`` that runs its
own and then, while the frame is an HD one, converts what it wrote from the
display colour to the scene value the tone curve maps back to that colour
(:data:`CLASSIC_GLSL`, the inverse of the compose pass's curve at the map's
base exposure). The picture of a sprite is then what it always was; only the
grade (exposure compensation, tint, contrast, saturation, vignette, bloom)
moves it, as it moves everything.

Whether a frame is an HD one is not per-program state: every rewritten
program reads one small uniform block (``FioHdr``), so entering and leaving
an HD frame -- or a reflection probe's classic capture inside one -- is one
buffer update.

Programs that read the frame back (glass, water, wet decals: ``sceneColor``)
read scene values; their reads are routed through ``fioSceneTex`` so their
own maths stays in display colours.

None of this exists at ``graphics = low``: sources reach the compiler
untouched.
"""

import re

import numpy as np

from engine import hd_graphics

#: Uniform block binding point of ``FioHdr`` (the light block uses 0).
HDR_BLOCK_BINDING = 3

#: Display white is held a little under the curve's asymptote, where the
#: inverse runs away: a white sprite stays white to within a few counts and
#: does not turn into a light source for the bloom.
_DISPLAY_CEILING = 0.97

CLASSIC_GLSL = """
// ---- classic programs in an HD frame (engine/renderer/hd_high/hdr.py) ----
layout(std140) uniform FioHdr {
    vec4 fioHdr;       // x 1 in an HD frame, y 1 / base exposure, z base exposure, w unused
};
const float FIO_TONE_WHITE2 = %(white2).4f;
// The compose pass's tone curve (extended Reinhard), per channel.
vec3 fioTone(vec3 x) {
    return clamp(x * (1.0 + x / FIO_TONE_WHITE2) / (1.0 + x), 0.0, 1.0);
}
// Display colour -> the gamma-encoded scene value the compose pass's tone
// curve (at the base exposure) brings back to that colour.
vec3 fioDisplayToScene(vec3 c) {
    vec3 f = min(pow(max(c, vec3(0.0)), vec3(2.2)), vec3(%(ceiling).2f));
    vec3 b = vec3(1.0) - f;
    vec3 x = (-b + sqrt(b * b + 4.0 * f / FIO_TONE_WHITE2)) * (FIO_TONE_WHITE2 * 0.5);
    return pow(max(x, vec3(0.0)) * fioHdr.y, vec3(1.0 / 2.2));
}
// The reverse: a value read from the HD frame as the display colour it
// will be shown as.
vec3 fioSceneToDisplay(vec3 c) {
    return pow(fioTone(pow(max(c, vec3(0.0)), vec3(2.2)) * fioHdr.z), vec3(1.0 / 2.2));
}
vec4 fioSceneTex(sampler2D s, vec2 uv) {
    vec4 c = texture(s, uv);
    return fioHdr.x > 0.5 ? vec4(fioSceneToDisplay(c.rgb), c.a) : c;
}
vec4 fioSceneTexLod(sampler2D s, vec2 uv, float lod) {
    vec4 c = textureLod(s, uv, lod);
    return fioHdr.x > 0.5 ? vec4(fioSceneToDisplay(c.rgb), c.a) : c;
}
""" % dict(white2=hd_graphics.TONE_WHITE ** 2, ceiling=_DISPLAY_CEILING)

_MAIN_RE = re.compile(r"void\s+main\s*\(\s*(?:void)?\s*\)\s*\{")
_OUT_RE = re.compile(r"^\s*(?:layout\s*\([^)]*\)\s*)?out\s+vec4\s+(\w+)\s*;", re.MULTILINE)
_VERSION_RE = re.compile(r"^\s*#version[^\n]*\n", re.MULTILINE)
_PRECISION_RE = re.compile(r"^\s*precision\s+\w+\s+float\s*;[^\n]*\n", re.MULTILINE)

#: Samplers that hold a copy of the frame being drawn.
SCENE_SAMPLERS = ("sceneColor",)


def display_to_scene(colour, base_exposure):
    """:data:`CLASSIC_GLSL`'s ``fioDisplayToScene`` on the CPU, returning
    *linear* scene rgb (for a colour handed to a surface program as a
    uniform: fog, the clear colour)."""
    white2 = hd_graphics.TONE_WHITE ** 2
    f = np.minimum(np.clip(np.asarray(colour, dtype=np.float64), 0.0, None) ** 2.2,
                   _DISPLAY_CEILING)
    b = 1.0 - f
    x = (-b + np.sqrt(b * b + 4.0 * f / white2)) * (white2 * 0.5)
    scene = np.clip(x, 0.0, None) / max(float(base_exposure), 1e-6)
    return tuple(float(v) for v in scene)


def classic_source(source):
    """*source* (a fragment shader that writes display colours to one
    ``out vec4``) with the HD conversion added, or *source* itself when it
    does not have that shape (a depth-only program, say)."""
    if not source or 'fioClassicMain' in source or 'fioLegacyMain' in source:
        return source
    main = _MAIN_RE.search(source)
    outputs = _OUT_RE.findall(source)
    if main is None or len(outputs) != 1:
        return source
    output = outputs[0]
    # The block and its functions go after the version and precision lines,
    # before anything that might call them.
    head = 0
    for pattern in (_VERSION_RE, _PRECISION_RE):
        found = pattern.search(source, head)
        if found is not None and found.start() < main.start():
            head = max(head, found.end())
    body = (source[:main.start()] + "void fioClassicMain() {" + source[main.end():])
    main_shift = len("void fioClassicMain() {") - (main.end() - main.start())
    del main_shift
    for sampler in SCENE_SAMPLERS:
        if re.search(r"uniform\s+(?:\w+\s+)?sampler2D\s+%s\s*;" % sampler, body):
            body = re.sub(r"\btextureLod\s*\(\s*%s\s*," % sampler,
                          "fioSceneTexLod(%s," % sampler, body)
            body = re.sub(r"\btexture\s*\(\s*%s\s*," % sampler,
                          "fioSceneTex(%s," % sampler, body)
    body = body[:head] + CLASSIC_GLSL + body[head:]
    return body + """
void main() {
    fioClassicMain();
    if (fioHdr.x > 0.5) %(out)s.rgb = fioDisplayToScene(%(out)s.rgb);
}
""" % dict(out=output)
