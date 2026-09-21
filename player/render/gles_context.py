"""OpenGL ES shader compilation helpers.

Thin wrappers over PyOpenGL that compile the translated GLES shader set into
programs, with readable error reporting (the driver's info log). PyOpenGL is
imported lazily so importing this module never requires a GL binding — only
:func:`compile_program` and friends do.

On Android, PyOpenGL's ``OpenGL.GL`` maps onto the device's libGLESv2 through
the ES-compatible entry points that ES 3.x shares with desktop GL, so the same
calls used here run on the phone.
"""

from __future__ import annotations

from typing import Dict, Tuple


class GLESContextError(RuntimeError):
    """Raised on shader compile / program link failure, with the info log."""


def _gl():
    import OpenGL.GL as gl  # lazy
    return gl


def compile_shader(source: str, shader_type) -> int:
    gl = _gl()
    shader = gl.glCreateShader(shader_type)
    gl.glShaderSource(shader, source)
    gl.glCompileShader(shader)
    if not gl.glGetShaderiv(shader, gl.GL_COMPILE_STATUS):
        log = gl.glGetShaderInfoLog(shader)
        if isinstance(log, bytes):
            log = log.decode("utf-8", "replace")
        gl.glDeleteShader(shader)
        raise GLESContextError(f"Shader compile failed:\n{log}\n--- source ---\n{source}")
    return shader


def compile_program(vertex_src: str, fragment_src: str) -> int:
    """Compile + link a vertex/fragment pair into a program id."""
    gl = _gl()
    vs = compile_shader(vertex_src, gl.GL_VERTEX_SHADER)
    fs = compile_shader(fragment_src, gl.GL_FRAGMENT_SHADER)
    program = gl.glCreateProgram()
    gl.glAttachShader(program, vs)
    gl.glAttachShader(program, fs)
    gl.glLinkProgram(program)
    # Shaders can be flagged for deletion once linked.
    gl.glDeleteShader(vs)
    gl.glDeleteShader(fs)
    if not gl.glGetProgramiv(program, gl.GL_LINK_STATUS):
        log = gl.glGetProgramInfoLog(program)
        if isinstance(log, bytes):
            log = log.decode("utf-8", "replace")
        gl.glDeleteProgram(program)
        raise GLESContextError(f"Program link failed:\n{log}")
    return program


def build_program_set(
    shader_sources: Dict[str, str],
    shader_map: Dict[str, Tuple[str, str]],
) -> Dict[str, int]:
    """Compile every logical shader in ``shader_map`` into a program.

    ``shader_sources`` is the translated GLES source dict (from
    :func:`player.gles_shaders.build_gles_shader_set`); ``shader_map`` pairs a
    logical name to its ``(vert_file, frag_file)``. Shaders whose sources are
    absent (e.g. the not-yet-integrated ``procedural`` pair) are skipped with a
    warning rather than aborting the whole set.
    """
    programs: Dict[str, int] = {}
    for name, (vert_file, frag_file) in shader_map.items():
        vsrc = shader_sources.get(vert_file)
        fsrc = shader_sources.get(frag_file)
        if vsrc is None or fsrc is None:
            print(f"[GLES] skipping '{name}': missing {vert_file}/{frag_file}")
            continue
        try:
            programs[name] = compile_program(vsrc, fsrc)
        except GLESContextError as exc:
            print(f"[GLES] shader '{name}' failed to build: {exc}")
    return programs
