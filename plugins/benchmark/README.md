# Benchmark Plugin

The Fio Benchmark plugin provides an in-engine performance testing environment for measuring Fio's real-world rendering and world-simulation performance.

Primarily designed to test your worlds during development

It also includes tests designed to benchmark the **actual Fio runtime and editor viewport**


## What it benchmarks

The benchmark plugin can exercise Fio using real maps and the normal runtime systems, including:

* World loading
* Rendering
* Visibility and distance culling
* Brush and entity rendering
* PlayerStart-based camera movement
* World traversal
* Large and dense scenes
* Frame-time behaviour under sustained load

The benchmark is intended to answer a practical question:

> **How fast does Fio actually run when processing a real world?**

This makes benchmark results useful for tracking regressions as the engine develops.

## In-engine benchmarking

The benchmark operates against the existing Fio `MainWindow` and `QtGameView` rather than creating a second ordinary Fio instance for normal map stress tests.

This is important because the benchmark should measure the same rendering and world path that users experience in the editor's Play mode.

The benchmark does not introduce a separate `benchmark_mode` into the engine. Benchmarking is implemented as a plugin-level capability rather than a special runtime architecture.

## Benchmark phases

The benchmark world uses the map's `PlayerStart` as its initial reference point.

A typical benchmark consists of multiple camera phases designed to exercise different parts of the world:

1. **PlayerStart orbit**

   The camera begins at the PlayerStart and performs a controlled orbit around the starting area.

2. **World traversal**

   The benchmark continues through a larger 360° camera sweep, exposing additional geometry and entities to the renderer and visibility system.

This provides a repeatable workload while still exercising the real world.

## Running a benchmark

The benchmark command accepts a duration and repeat count:

```text
benchmark <seconds> <repeat>
```

For example:

```text
benchmark 30 3
```

runs a 30-second benchmark three times.

Repeating the same workload makes it easier to identify unstable results and distinguish a persistent performance change from normal frame-time variation.

## What makes the results useful

Fio's benchmark is intended to work alongside **SysMon**, which exposes runtime information such as:

* FPS
* Frame time
* Visible brushes
* Culled brushes
* Triangle counts
* VRAM usage

This means a benchmark can be correlated with the actual workload being processed by the engine.

For example, an FPS change can be examined alongside visibility and triangle counts to determine whether a performance change is associated with rendering workload, culling behaviour, or another part of the runtime.

## Real Fio workload

The benchmark deliberately does not replace Fio's production systems with benchmark-specific implementations.

The purpose is to exercise the same systems used during normal operation:

```text
Map
 │
 ├── World / entities
 ├── Visibility & distance culling
 ├── Renderer
 ├── Logic/runtime systems
 └── QtGameView
          │
          ▼
      Benchmark
```

This is particularly important for Fio because many of its performance optimisations operate on the relationship between world size, visibility, entity distance and rendering workload.

A benchmark that bypassed those systems would not provide useful information about the actual engine.

## Reproducibility

Benchmarks should use the same:

* Map
* Camera path
* Benchmark duration
* Repeat count
* Rendering configuration

when comparing results between Fio versions.

For meaningful regression testing, compare repeated runs rather than relying on a single instantaneous FPS value.


## Design philosophy

The benchmark plugin follows the same principle as the rest of Fio:

> **Benchmark the world, not an artificial benchmark scene.**

Fio is intended to run executable worlds rather than simply display static editor geometry. Performance testing therefore needs to account for the complete runtime workload.

The benchmark plugin exists to make that workload repeatable, measurable and comparable between versions.
