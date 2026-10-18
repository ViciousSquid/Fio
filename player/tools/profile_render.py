"""Reproducible off-device player workload trace; these timings are not phone FPS.

Run from the Android-private staged runtime, with PYOPENGL_PLATFORM=egl,
EGL_PLATFORM=surfaceless and SDL_AUDIODRIVER=dummy.
"""
import argparse
from collections import Counter
from contextlib import ExitStack
import ctypes
import json
from pathlib import Path
import time
from unittest.mock import patch


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('package')
    parser.add_argument('--quality', choices=('reference','mobile'), default='mobile')
    parser.add_argument('--frames', type=int, default=120)
    parser.add_argument('--width', type=int, default=640)
    parser.add_argument('--height', type=int, default=288)
    parser.add_argument('--output', required=True)
    args = parser.parse_args(argv)
    if min(args.frames, args.width, args.height) < 1:
        parser.error('Frame count and dimensions must be positive')
    from OpenGL import EGL, GL
    import numpy as np
    from player.app import FioPlayerApp
    from player.input.state import InputState
    from player.runtime.fiopak import FioPackage
    from player.render.renderer import GLESRenderer
    from player.render.quality import resolve

    display = EGL.eglGetDisplay(EGL.EGL_DEFAULT_DISPLAY)
    EGL.eglInitialize(display, ctypes.c_int(), ctypes.c_int())
    attributes = (ctypes.c_int * 13)(EGL.EGL_SURFACE_TYPE,EGL.EGL_PBUFFER_BIT,
        EGL.EGL_RENDERABLE_TYPE,0x40,EGL.EGL_RED_SIZE,8,EGL.EGL_GREEN_SIZE,8,
        EGL.EGL_BLUE_SIZE,8,EGL.EGL_DEPTH_SIZE,24,EGL.EGL_NONE)
    config, count = EGL.EGLConfig(), ctypes.c_int()
    EGL.eglChooseConfig(display,attributes,ctypes.byref(config),1,count)
    if count.value != 1:
        raise RuntimeError('No GLES 3 pbuffer configuration')
    EGL.eglBindAPI(EGL.EGL_OPENGL_ES_API)
    context = EGL.eglCreateContext(display,config,EGL.EGL_NO_CONTEXT,
        (ctypes.c_int * 3)(EGL.EGL_CONTEXT_CLIENT_VERSION,3,EGL.EGL_NONE))
    surface = EGL.eglCreatePbufferSurface(display,config,
        (ctypes.c_int * 5)(EGL.EGL_WIDTH,args.width,EGL.EGL_HEIGHT,args.height,EGL.EGL_NONE))
    EGL.eglMakeCurrent(display,surface,surface,context)
    app = FioPlayerApp(FioPackage.open(args.package))
    frames, counts = [], Counter()
    try:
        app.renderer = GLESRenderer(quality=resolve(args.quality))
        app.renderer.on_gl_ready()
        app.renderer.resize(args.width,args.height)
        app._load_level(app.package.start_map_path())
        with ExitStack() as patches:
            for name in ('glDrawArrays','glGetUniformLocation','glGenerateMipmap'):
                original = getattr(GL,name)
                def counted(*values, _name=name, _original=original, **kwargs):
                    counts[_name] += 1
                    return _original(*values,**kwargs)
                patches.enter_context(patch.object(GL,name,counted))
            for index in range(args.frames):
                counts.clear()
                start = time.perf_counter()
                app.world.step(1/60,InputState(),app.cam_yaw,app.cam_pitch)
                simulation_end = time.perf_counter()
                app.renderer.render(app._render_state())
                submitted = time.perf_counter()
                # Benchmark-only completion fence; never used in the live player.
                GL.glFinish()
                completed = time.perf_counter()
                frames.append(dict(frame=index, **counts, **app.renderer.frame_stats,
                    simulation_cpu_ms=(simulation_end-start)*1000,
                    render_submit_ms=(submitted-simulation_end)*1000,
                    render_complete_ms=(completed-simulation_end)*1000))
        assert GL.glGetError() == GL.GL_NO_ERROR
        result = dict(quality=args.quality, surface=[args.width,args.height],
            gl_renderer=GL.glGetString(GL.GL_RENDERER).decode(),
            total_batches=len(app.renderer._batches),
            missing_images=sorted(app.renderer._images.missing), frames=frames,
            render_complete_p50_ms=float(np.percentile([f['render_complete_ms'] for f in frames],50)),
            render_complete_p95_ms=float(np.percentile([f['render_complete_ms'] for f in frames],95)))
        Path(args.output).write_text(json.dumps(result,indent=2)+'\n')
        print(f"Recorded {len(frames)} {args.quality} frames to {args.output}; "
              f"p50/p95 completed render {result['render_complete_p50_ms']:.2f}/"
              f"{result['render_complete_p95_ms']:.2f} ms on {result['gl_renderer']}")
    finally:
        app._on_shutdown()
        EGL.eglMakeCurrent(display,EGL.EGL_NO_SURFACE,EGL.EGL_NO_SURFACE,EGL.EGL_NO_CONTEXT)
        EGL.eglDestroySurface(display,surface)
        EGL.eglDestroyContext(display,context)
        EGL.eglTerminate(display)


if __name__ == '__main__':
    main()
