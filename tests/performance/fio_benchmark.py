                "delay": 0.0,
                "fire_once": False,
            }
            for target in targets
            if target != i
        ]

    return data


def _run_monster_apocalypse():
    """Optional final test: deliberately overload the complete Fio stack."""
    data = _generate_monster_apocalypse()
    state = _materialize_generated_map(data)
    brushes, things = state.brushes, state.things

    results = []
    for mode, width, height in (
        ("windowed-sized", 1280, 720),
        ("fullscreen-sized", 1920, 1080),
    ):
        glh.reset_texture_cache()
        with glh.GLTestContext(width, height) as context:
            renderer = glh.make_renderer()
            try:
                # Very short warmup: this is explicitly a stress-to-failure
                # test, not a polished benchmark workload.
                samples, sysmon_metrics = _render_sample_set(
                    renderer, context, brushes, things, warmup=1, samples=10
                )
                mean = statistics.fmean(samples)
                results.append(_timing_result(
                    "FINAL_MONSTER_APOCALYPSE_%s" % mode,
                    "MAX procedural world / 1000 monsters / 1000 relays / dense I/O",
                    samples,
                    mode=mode,
                    monster_count=1000,
                    brush_count=len(brushes),
                    entity_count=len(things),
                    resolution="%dx%d" % (width, height),
                    average_fps=sysmon_metrics["fps"],
                    sysmon=sysmon_metrics,
                ))
            finally:
                try:
                    renderer.cleanup()
                except Exception:
                    pass

    # Do not create a second MainWindow here.  This benchmark is launched
    # from Tools > Benchmark, so opening a fresh Fio instance for Play Mode
    # would benchmark a different application instance than the one the user
    # started the test from.  The renderer stress above is the apocalypse
    # measurement; live Play Mode benchmarks belong to the existing MainWindow.

    glh.reset_texture_cache()
    return results



def run_live_renderer_sample(window, duration=1.0, warmup=0.75):
    """Measure the already-running Fio viewport through its real Qt event loop.

    No second MainWindow, QOpenGLWidget, OpenGL context, or Renderer_F is
    created. The existing viewport paints normally while the benchmark dialog
    is open, and SysMon records the frames that actually reached paintGL().
    """
    from PyQt5.QtWidgets import QApplication

    view = window.view_3d
    app = QApplication.instance()