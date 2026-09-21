"""Adapt the engine's desktop OpenGL 3.3 shaders to OpenGL ES 3.x.

Fio's renderer targets desktop ``#version 330 core`` GLSL. Android's graphics
API is OpenGL ES, whose shading language (GLSL ES 3.00, used with an ES 3.x
context) is a close but not identical dialect. The concepts the renderer relies
on — VBOs/VAOs, ``in``/``out`` stage I/O, ``layout(location=)`` attributes,
custom fragment outputs, ``texture()`` overloads, ``sampler3D``/``samplerCube``,
array constructors, ``gl_FragDepth`` — all exist in GLSL ES 3.00, so the port is
a translation rather than a rewrite.

The concrete differences this module handles:

* **Version directive** — ``#version 330 core`` becomes ``#version 300 es``,
  which must be the very first line of the source.
* **Fragment float precision** — the ES fragment language has *no* default
  precision for ``float``; a shader that omits ``precision <p> float;`` fails to
  compile. We inject ``precision highp float;`` when the source lacks one.
* **Integer / new-sampler precision** — GLSL ES 3.00 predeclares no default
  precision for ``int`` in some cases, nor for the samplers added in ES 3.00
  (notably ``sampler3D``); ``samplerCube`` defaults to ``lowp`` which is useless
  for the shadow depth cube-maps. We inject ``highp`` defaults for the sampler
  types each shader actually uses.

Everything here is pure text manipulation with no GL calls, so it is unit
testable off-device. :func:`build_gles_shader_set` pulls the canonical shader
strings straight from :mod:`engine.shaders` and returns a ready-to-compile GLES
set, keeping a single source of truth for shader logic across desktop and mobile.
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, Tuple

GLES_VERSION_DIRECTIVE = "#version 300 es"

# Matches a desktop version line: "#version 330", "#version 330 core", etc.
_VERSION_RE = re.compile(r"^[ \t]*#version[ \t]+\d+(?:[ \t]+\w+)?[ \t]*$", re.MULTILINE)

# Detects an existing "precision <p> <type>;" for a given type.
def _has_precision(src: str, gl_type: str) -> bool:
    pattern = rf"\bprecision\s+(?:lowp|mediump|highp)\s+{re.escape(gl_type)}\s*;"
    return re.search(pattern, src) is not None


def _uses_type(src: str, gl_type: str) -> bool:
    return re.search(rf"\b{re.escape(gl_type)}\b", src) is not None


VERTEX = "vertex"
FRAGMENT = "fragment"


def translate(src: str, stage: str) -> str:
    """Translate one desktop GLSL 3.30 shader stage to GLSL ES 3.00.

    Parameters
    ----------
    src:
        The desktop shader source string.
    stage:
        ``"vertex"`` or ``"fragment"`` — controls which default precision
        qualifiers are required.

    The transformation is idempotent-ish: re-running it on already-ES source
    leaves the version directive alone and does not stack duplicate precision
    lines.
    """
    if stage not in (VERTEX, FRAGMENT):
        raise ValueError(f"stage must be 'vertex' or 'fragment', got {stage!r}")

    src = src.replace("\r\n", "\n").replace("\r", "\n")

    # 1. Normalise the version directive to ES and strip it from the body so we
    #    can re-emit it as the guaranteed first line.
    body = _VERSION_RE.sub("", src, count=1).lstrip("\n")

    # 2. Work out which default-precision statements must be injected. Only add
    #    a default when the shader both needs it and does not already set it.
    precision_lines = list(_required_precision(body, stage))

    # 3. Reassemble: version first, then injected precisions, then body.
    header = [GLES_VERSION_DIRECTIVE]
    header.extend(precision_lines)
    return "\n".join(header) + "\n" + body


def _required_precision(body: str, stage: str) -> Iterable[str]:
    # Fragment shaders have no predeclared float precision in ES — mandatory.
    if stage == FRAGMENT and not _has_precision(body, "float"):
        yield "precision highp float;"

    # Integer precision: predeclared in ES, but declaring highp is harmless and
    # avoids mediump truncation of the loop counters / light indices.
    if stage == FRAGMENT and _uses_type(body, "int") and not _has_precision(body, "int"):
        yield "precision highp int;"

    # sampler3D (volumetric fog noise) has no predeclared default in ES 3.00.
    if _uses_type(body, "sampler3D") and not _has_precision(body, "sampler3D"):
        yield "precision highp sampler3D;"

    # samplerCube defaults to lowp in ES, but the shadow cube-maps store linear
    # depth and need highp to avoid catastrophic banding.
    if _uses_type(body, "samplerCube") and not _has_precision(body, "samplerCube"):
        yield "precision highp samplerCube;"


# ----------------------------------------------------------------------
# Engine integration
# ----------------------------------------------------------------------
def build_gles_shader_set(prefer_arm: bool = True) -> Dict[str, str]:
    """Return every engine shader translated to GLSL ES 3.00.

    Keys are the engine's shader filenames (``lit.vert``, ``textured.frag`` …)
    and values are the translated ES sources. Pulling from
    :data:`engine.shaders.DEFAULT_SHADERS` keeps shader *logic* defined in one
    place; this module only re-dialects it for the device.

    When ``prefer_arm`` is set, the lighter ``*_arm`` variants (fewer lights,
    no per-fragment precision churn) are also translated — those are the sane
    default for mobile GPUs.
    """
    from engine.shaders import DEFAULT_SHADERS  # local import: engine is heavy

    out: Dict[str, str] = {}
    for filename, source in DEFAULT_SHADERS.items():
        stage = _stage_for_filename(filename)
        if stage is None:
            out[filename] = source  # unknown extension — pass through untouched
            continue
        out[filename] = translate(source, stage)
    return out


def build_gles_shader_map() -> Dict[str, Tuple[str, str]]:
    """The engine's shader-name -> (vert_file, frag_file) registry.

    Re-exported so the mobile renderer can pair translated sources by logical
    shader name (``"lit"``, ``"water"`` …) exactly as the desktop renderer does.
    """
    from engine.shaders import SHADER_MAP

    return dict(SHADER_MAP)


def _stage_for_filename(filename: str):
    lower = filename.lower()
    if lower.endswith(".vert") or "vert" in lower.rsplit(".", 1)[0].split("_"):
        return VERTEX
    if lower.endswith(".frag") or "frag" in lower.rsplit(".", 1)[0].split("_"):
        return FRAGMENT
    if lower.endswith(".vert.glsl") or lower.endswith("_vert.glsl"):
        return VERTEX
    if lower.endswith(".frag.glsl") or lower.endswith("_frag.glsl"):
        return FRAGMENT
    return None
