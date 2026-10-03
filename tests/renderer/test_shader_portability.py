"""Shader portability: every engine shader must compile on strict drivers.

A shader that one driver accepts can still fail on another. Intel's GLSL
compiler rejected ``glass.frag`` with "overloaded functions must have the same
return type" because it defined ``float noise2(vec2)``: GLSL 3.30 still ships
the deprecated built-ins ``noise1``..``noise4`` (``noise2`` returns a
``vec2``), so the user function clashed with a built-in. ARM drivers let it
through.

Two layers of guard:

* Pure-Python checks that always run: no shader may define a function named
  like a GLSL built-in, or use an identifier that later GLSL versions (or
  GLSL ES) reserve - ``patch``, ``sample``, ``centroid`` and friends.
* When ``glslangValidator`` (the Khronos reference compiler) is installed,
  every shader is compiled with it as desktop GLSL 3.30 and, through the
  player's translator, as GLSL ES 3.00. It reproduces the Intel error above.
"""

import os
import re
import shutil
import subprocess
import tempfile

import pytest

from engine import shaders

SOURCES = {name: src for name, src in shaders.DEFAULT_SHADERS.items()
           if name.endswith(('.vert', '.frag'))}

#: GLSL built-in functions a user shader could plausibly redefine by accident.
#: Redefining one with a different return type is a hard error on strict
#: compilers (and redefining one at all is an error in GLSL ES).
BUILTIN_FUNCTIONS = {
    'noise1', 'noise2', 'noise3', 'noise4',
    'radians', 'degrees', 'sin', 'cos', 'tan', 'asin', 'acos', 'atan',
    'sinh', 'cosh', 'tanh', 'asinh', 'acosh', 'atanh', 'pow', 'exp', 'log',
    'exp2', 'log2', 'sqrt', 'inversesqrt', 'abs', 'sign', 'floor', 'trunc',
    'round', 'roundEven', 'ceil', 'fract', 'mod', 'modf', 'min', 'max',
    'clamp', 'mix', 'step', 'smoothstep', 'isnan', 'isinf', 'length',
    'distance', 'dot', 'cross', 'normalize', 'faceforward', 'reflect',
    'refract', 'transpose', 'determinant', 'inverse', 'outerProduct',
    'matrixCompMult', 'lessThan', 'greaterThan', 'equal', 'notEqual', 'any',
    'all', 'not', 'texture', 'textureLod', 'texelFetch', 'textureSize',
    'textureProj', 'textureGrad', 'textureOffset', 'dFdx', 'dFdy', 'fwidth',
    'floatBitsToInt', 'intBitsToFloat', 'packUnorm2x16', 'unpackUnorm2x16',
    'fma', 'frexp', 'ldexp', 'bitCount', 'findLSB', 'findMSB',
}

#: Identifiers reserved by GLSL 4.x or GLSL ES 3.00 (or reserved outright)
#: that are valid-looking variable names. A driver compiling for a newer
#: language level, or the GLES translation, rejects them.
RESERVED_IDENTIFIERS = {
    'patch', 'sample', 'centroid', 'subroutine', 'precise', 'buffer',
    'shared', 'coherent', 'volatile', 'restrict', 'readonly', 'writeonly',
    'resource', 'common', 'partition', 'active', 'filter', 'input', 'output',
    'superp', 'half', 'fixed', 'long', 'short', 'double', 'unsigned',
    'interface', 'external', 'cast', 'namespace', 'using', 'template',
    'this', 'goto', 'inline', 'noinline', 'public', 'static', 'extern',
    'sizeof', 'union', 'enum', 'typedef', 'class', 'asm', 'packed',
    'invariant', 'smooth', 'flat', 'noperspective',
}

TYPES = (r'(?:float|int|uint|bool|[biu]?vec[234]|mat[234](?:x[234])?|'
         r'sampler\w*)')
FUNCTION_DEF = re.compile(
    r'^\s*(?:(?:highp|mediump|lowp)\s+)?' + TYPES + r'\s+(\w+)\s*\([^;]*\)\s*\{',
    re.MULTILINE)
DECLARATION = re.compile(
    r'(?:^|[;{(,]\s*|\n\s*)(?:(?:const|in|out|inout|uniform|highp|mediump|lowp)\s+)*'
    + TYPES + r'\s+(\w+)')


def _strip_comments(src):
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.DOTALL)
    return re.sub(r'//[^\n]*', '', src)


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_shader_redefines_a_builtin_function(name):
    defined = set(FUNCTION_DEF.findall(_strip_comments(SOURCES[name])))
    clashes = sorted(defined & BUILTIN_FUNCTIONS)
    assert not clashes, (
        f"{name} defines {clashes}, which are GLSL built-ins; strict drivers "
        f"(Intel) reject the redefinition - rename the function")


@pytest.mark.parametrize("name", sorted(SOURCES))
def test_no_shader_uses_a_reserved_identifier(name):
    declared = set(DECLARATION.findall(_strip_comments(SOURCES[name])))
    bad = sorted(declared & RESERVED_IDENTIFIERS)
    assert not bad, (
        f"{name} declares {bad}, reserved in later GLSL / GLSL ES - rename")


def test_the_checks_catch_the_intel_failure():
    """The guard would have caught the glass shader that broke on Intel."""
    old_glass = """#version 330 core
highp float hash21(highp vec2 p) { return fract(p.x * p.y); }
highp float noise2(highp vec2 p) {
    return hash21(p);
}
void main() {}
"""
    assert 'noise2' in set(FUNCTION_DEF.findall(old_glass)) & BUILTIN_FUNCTIONS
    assert 'patch' in set(DECLARATION.findall("void f() { float patch = 1.0; }"))


# ---------------------------------------------------------------------------
# Reference compiler
# ---------------------------------------------------------------------------

GLSLANG = shutil.which('glslangValidator')
needs_glslang = pytest.mark.skipif(
    GLSLANG is None, reason="glslangValidator (Khronos reference compiler) not installed")


def _variants(name, src):
    """The source as each backend compiles it."""
    out = {'desktop': src}
    ubo = shaders.light_ubo_source(src)
    if ubo != src:
        out['desktop+light-ubo'] = ubo
    try:
        from player import gles_shaders
    except Exception:           # the player package is optional here
        return out
    stage = 'vertex' if name.endswith('.vert') else 'fragment'
    for key in list(out):
        out[key + ' (GLSL ES)'] = gles_shaders.translate(out[key], stage)
    return out


def _compile(name, src):
    ext = '.vert' if name.endswith('.vert') else '.frag'
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, 'shader' + ext)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(src)
        result = subprocess.run([GLSLANG, path], capture_output=True, text=True)
    errors = [line for line in result.stdout.splitlines() if 'ERROR' in line]
    return result.returncode, errors


@needs_glslang
@pytest.mark.parametrize("name", sorted(SOURCES))
def test_every_shader_compiles_with_the_reference_compiler(name):
    for variant, src in _variants(name, SOURCES[name]).items():
        code, errors = _compile(name, src)
        assert code == 0, f"{name} [{variant}] failed:\n" + "\n".join(errors)


@needs_glslang
def test_the_reference_compiler_reproduces_the_intel_failure():
    code, errors = _compile('glass.frag', """#version 330 core
out vec4 FragColor;
highp float noise2(highp vec2 p) { return fract(p.x * p.y); }
void main() { FragColor = vec4(noise2(gl_FragCoord.xy)); }
""")
    assert code != 0
    assert any('overloaded functions must have the same return type' in e
               for e in errors)
