"""Real ES 3 context and pixel checks, without a window or Android device.

Run with PYOPENGL_PLATFORM=egl EGL_PLATFORM=surfaceless python -m player.tools.smoke_gles.
Requires Mesa EGL/GLES (software rendering is sufficient).
"""
import ctypes
import io
import json
import zipfile


def main():
    import sys
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--desktop', action='store_true')
    parser.add_argument('--quality', choices=('reference', 'mobile'), default='reference')
    parser.add_argument('--fixture', required=True, help='World exported by the desktop exporter')
    parser.add_argument('--evidence', help='Directory for rendering pixel-check images')
    args = parser.parse_args()
    desktop = args.desktop
    import numpy as np
    from OpenGL import EGL, GL
    from PIL import Image
    from player.runtime.fiopak import FioPackage
    from player.render.renderer import GLESRenderer

    display = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
    major, minor = ctypes.c_int(), ctypes.c_int()
    EGL.eglInitialize(display, major, minor)
    attributes = (ctypes.c_int * 13)(
        EGL.EGL_SURFACE_TYPE, EGL.EGL_PBUFFER_BIT,
        EGL.EGL_RENDERABLE_TYPE, 8 if desktop else 0x40,  # EGL_OPENGL_ES3_BIT
        EGL.EGL_RED_SIZE, 8, EGL.EGL_GREEN_SIZE, 8, EGL.EGL_BLUE_SIZE, 8,
        EGL.EGL_DEPTH_SIZE, 24, EGL.EGL_NONE)
    config, count = EGL.EGLConfig(), ctypes.c_int()
    EGL.eglChooseConfig(display, attributes, ctypes.byref(config), 1, count)
    if count.value != 1:
        raise RuntimeError("No ES 3 pbuffer configuration")
    EGL.eglBindAPI(EGL.EGL_OPENGL_API if desktop else EGL.EGL_OPENGL_ES_API)
    context_attributes = ((ctypes.c_int * 7)(0x3098, 3, 0x30FB, 3, 0x30FD, 1, EGL.EGL_NONE)
                          if desktop else (ctypes.c_int * 3)(EGL.EGL_CONTEXT_CLIENT_VERSION, 3, EGL.EGL_NONE))
    context = EGL.eglCreateContext(display, config, EGL.EGL_NO_CONTEXT, context_attributes)
    surface = EGL.eglCreatePbufferSurface(display, config,
        (ctypes.c_int * 5)(EGL.EGL_WIDTH, 128, EGL.EGL_HEIGHT, 128, EGL.EGL_NONE))
    EGL.eglMakeCurrent(display, surface, surface, context)
    try:
        print(GL.glGetString(GL.GL_VERSION).decode())
        from player.render.quality import resolve
        renderer = GLESRenderer(gles=not desktop, quality=resolve(args.quality))
        renderer.on_gl_ready()
        renderer.resize(128, 128)

        def recreate_context():
            nonlocal context
            EGL.eglMakeCurrent(display, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, EGL.EGL_NO_CONTEXT)
            EGL.eglDestroyContext(display, context)
            context = EGL.eglCreateContext(display, config, EGL.EGL_NO_CONTEXT,
                                          context_attributes)
            EGL.eglMakeCurrent(display, surface, surface, context)
            renderer.on_gl_ready()
            renderer.resize(128, 128)

        renderer.render()
        empty = np.frombuffer(GL.glReadPixels(0, 0, 128, 128, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE), dtype=np.uint8)
        texture = io.BytesIO()
        Image.new("RGB", (2, 2), (255, 0, 0)).save(texture, format="PNG")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("metadata.json", json.dumps({"map_path": "maps/test.json"}))
            zf.writestr("maps/test.json", json.dumps({"brushes": [
                {"pos": [0, 0, -5], "size": [3, 3, 1], "textures": {"north": "red.png"}}
            ]}))
            zf.writestr("assets/textures/red.png", texture.getvalue())
        with FioPackage.from_bytes(archive.getvalue()) as package:
            renderer.load_scene(package.load_start_map(), package)
            state = {"cam_pos": (0, 0, 0), "cam_yaw": -90, "cam_pitch": 0}
            for reload_context in (False, True):
                if reload_context:
                    recreate_context()
                renderer.render(state)
                pixel = np.frombuffer(GL.glReadPixels(64, 64, 1, 1, GL.GL_RGBA, GL.GL_UNSIGNED_BYTE), dtype=np.uint8)
                assert abs(int(pixel[0])-26) <= 2 and pixel[1] < 2 and pixel[2] < 2, pixel  # desktop ambient is 0.1
                assert GL.glGetError() == GL.GL_NO_ERROR
                from player.input.touch import TouchControls
                from player.input.state import InputState
                renderer.render_overlay(TouchControls(), InputState(), 128, 128)
                assert GL.glGetError() == GL.GL_NO_ERROR
                renderer.render_ui({'hud': 'Health 100', 'paused': True}, 128, 128)
                assert GL.glGetError() == GL.GL_NO_ERROR
                renderer.render_ui({'hud': 'Health 100', 'paused': False}, 128, 128)
                assert GL.glGetError() == GL.GL_NO_ERROR
            assert np.ptp(empty) > 20, "Reference view was blank"
        from player.tools.check_rendering import run
        run(renderer, args.evidence)
        from player.tools.check_mobile_rendering import run as check_mobile
        check_mobile(renderer)
        from player.tools.check_water import run as check_water
        check_water(renderer, args.evidence, recreate_context)
        from player.world import PlayerWorld
        from player.render import glmath
        world = PlayerWorld({'terrain_data': {'enabled': True, 'solid': True, 'use_textures': False,
                             'min_chunk_x': 0, 'max_chunk_x': 0, 'min_chunk_z': 0, 'max_chunk_z': 0},
                             'brushes': [], 'things': []})
        try:
            renderer.load_scene(world.data, world=world)
            terrain_state = {'cam_pos': (128, 1000, 128),
                             'view': glmath.look_at((128, 1000, 128), (128, 0, 128), (0, 0, -1))}
            for _ in range(5):
                renderer.render(terrain_state)
            assert world.terrain.total_triangles > 0, 'Terrain drew no geometry'
            assert GL.glGetError() == GL.GL_NO_ERROR
        finally:
            world.close()
        import tempfile
        from pathlib import Path
        from player.app import FioPlayerApp
        from player.input.state import InputState
        with tempfile.TemporaryDirectory() as directory:
            path = Path(args.fixture)
            app = FioPlayerApp(FioPackage.open(path))
            app.renderer = renderer
            try:
                app._load_level(app.package.start_map_path())
                assert any(source[0] == 'model' for source in renderer._draw_sources.values()), 'Packaged OBJ not uploaded'
                app.world.step(1/30, InputState(), app.cam_yaw, 0)
                renderer.render(app._render_state())
                app.world.request_level_change('sub/second.json')
                app._advance_level()
                assert app.map_path == 'maps/sub/second.json'
                renderer.render(app._render_state())
                assert GL.glGetError() == GL.GL_NO_ERROR
            finally:
                app._on_shutdown()
        print("PASS: ZIP texture/model, shared terrain, level transition, HUD/menu, resource reload, GL errors")
    finally:
        EGL.eglMakeCurrent(display, EGL.EGL_NO_SURFACE, EGL.EGL_NO_SURFACE, EGL.EGL_NO_CONTEXT)
        EGL.eglDestroySurface(display, surface)
        EGL.eglDestroyContext(display, context)
        EGL.eglTerminate(display)


if __name__ == "__main__":
    main()
