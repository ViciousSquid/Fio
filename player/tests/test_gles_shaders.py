"""Tests for the GLSL 330 -> GLSL ES 300 translator (stdlib only)."""

import re
import unittest

from player import gles_shaders as gs


VERT_330 = """#version 330 core
precision highp float;
layout (location = 0) in vec3 aPos;
uniform mat4 model;
void main() { gl_Position = model * vec4(aPos, 1.0); }
"""

FRAG_330_NO_PRECISION = """#version 330 core
out vec4 FragColor;
uniform vec3 color;
void main() { FragColor = vec4(color, 1.0); }
"""

FRAG_330_WITH_SAMPLERS = """#version 330 core
precision mediump float;
out vec4 FragColor;
uniform sampler3D noiseTexture;
uniform samplerCube shadowMaps[4];
uniform int active_lights;
void main() {
    float n = texture(noiseTexture, vec3(0.0)).r;
    float s = texture(shadowMaps[0], vec3(0.0)).r;
    FragColor = vec4(n, s, float(active_lights), 1.0);
}
"""


class TestTranslate(unittest.TestCase):
    def test_version_directive_is_first_line(self):
        out = gs.translate(VERT_330, gs.VERTEX)
        self.assertEqual(out.splitlines()[0], "#version 300 es")

    def test_no_desktop_version_remains(self):
        out = gs.translate(VERT_330, gs.VERTEX)
        self.assertNotIn("330", out)
        self.assertNotIn("core", out)

    def test_fragment_without_precision_gets_one(self):
        out = gs.translate(FRAG_330_NO_PRECISION, gs.FRAGMENT)
        self.assertIn("precision highp float;", out)
        # It must appear before the first use of a float-typed construct.
        idx_prec = out.index("precision highp float;")
        idx_main = out.index("void main")
        self.assertLess(idx_prec, idx_main)

    def test_fragment_with_precision_not_duplicated(self):
        out = gs.translate(FRAG_330_WITH_SAMPLERS, gs.FRAGMENT)
        self.assertEqual(out.count("precision mediump float;"), 1)
        # We should not also add a highp float default on top of the mediump one.
        self.assertEqual(
            len(re.findall(r"precision\s+\w+\s+float\s*;", out)), 1
        )

    def test_sampler3d_gets_precision(self):
        out = gs.translate(FRAG_330_WITH_SAMPLERS, gs.FRAGMENT)
        self.assertIn("precision highp sampler3D;", out)

    def test_samplercube_gets_highp_precision(self):
        out = gs.translate(FRAG_330_WITH_SAMPLERS, gs.FRAGMENT)
        self.assertIn("precision highp samplerCube;", out)

    def test_int_precision_added_when_used(self):
        out = gs.translate(FRAG_330_WITH_SAMPLERS, gs.FRAGMENT)
        self.assertIn("precision highp int;", out)

    def test_body_preserved(self):
        out = gs.translate(VERT_330, gs.VERTEX)
        self.assertIn("layout (location = 0) in vec3 aPos;", out)
        self.assertIn("gl_Position = model * vec4(aPos, 1.0);", out)

    def test_invalid_stage_rejected(self):
        with self.assertRaises(ValueError):
            gs.translate(VERT_330, "geometry")

    def test_precision_lines_after_version(self):
        out = gs.translate(FRAG_330_WITH_SAMPLERS, gs.FRAGMENT)
        lines = out.splitlines()
        self.assertEqual(lines[0], "#version 300 es")
        # Every injected/existing precision directive precedes the body.
        first_code = next(
            i for i, l in enumerate(lines) if l.strip().startswith("out ")
        )
        for i, line in enumerate(lines):
            if line.strip().startswith("precision"):
                self.assertLess(i, first_code)


class TestEngineIntegration(unittest.TestCase):
    """Exercise the real engine shader set (engine.shaders is pure-stdlib)."""

    def setUp(self):
        try:
            self.shader_set = gs.build_gles_shader_set()
        except Exception as exc:  # engine import failed
            self.skipTest(f"engine.shaders unavailable: {exc}")

    def test_all_shaders_translated(self):
        self.assertIn("lit.vert", self.shader_set)
        self.assertIn("lit.frag", self.shader_set)
        self.assertIn("water.frag", self.shader_set)

    def test_every_source_starts_with_es_version(self):
        for name, src in self.shader_set.items():
            if not (name.endswith(".vert") or name.endswith(".frag")):
                continue
            self.assertEqual(
                src.splitlines()[0], "#version 300 es",
                msg=f"{name} did not get an ES version directive",
            )

    def test_no_fragment_shader_lacks_float_precision(self):
        for name, src in self.shader_set.items():
            if not name.endswith(".frag"):
                continue
            self.assertRegex(
                src, r"precision\s+(?:lowp|mediump|highp)\s+float\s*;",
                msg=f"{name} has no float precision (would fail ES compile)",
            )

    def test_fog_frag_has_sampler3d_precision(self):
        fog = self.shader_set.get("fog.frag")
        if fog is None:
            self.skipTest("fog.frag not in set")
        self.assertIn("precision highp sampler3D;", fog)

    def test_shader_map_available(self):
        smap = gs.build_gles_shader_map()
        self.assertIn("lit", smap)
        self.assertEqual(smap["lit"], ("lit.vert", "lit.frag"))


if __name__ == "__main__":
    unittest.main()
