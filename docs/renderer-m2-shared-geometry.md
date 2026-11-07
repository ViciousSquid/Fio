# Renderer M1/M2: angled brushes — measured results

Branch `3.1.0.0611_CANARY`. M1 `58cd5b9` (face state resolved once per mesh
build, sorted submission), M2 `2928554` + `0564458` + `5b26ede` (shared vertex
pages, buffer-texture transforms, batched draws; storage bounded under undo
and churn; mesh cache keyed by the mesh's complete input). Base is `126f8bb`.
Benchmarks were taken on `0564458`; `5b26ede` changes only which records a
mesh is shared between (see "Mesh cache key").

All timings are **Mesa software GL on x86-64** (llvmpipe, one run per
configuration, 960×540). They show CPU-side submission cost and relative
behaviour; they are not hardware-GPU or ARM results. llvmpipe's
`GL_TIME_ELAPSED` reflects its rasteriser threads and was inconsistent between
runs, so GPU timer values are not reported as GPU cost.

## Reproduce

```bash
xvfb-run -a python tools/bench_geo_passes.py --batching elements --json passes.json
xvfb-run -a python tools/bench_geo_edit.py --grid 12 24 48 --batching off elements multidraw --json edit.json
xvfb-run -a python tools/bench_geo_edit.py --map maps/_SHOWCASE.json --batching off elements multidraw
```

`--tree <checkout>` runs the same harness against another revision (a
`git worktree` of `58cd5b9` or `126f8bb`). The worlds are generated
deterministically (`angled_world`): 140, 572 and 2,300 angled brushes plus a
floor.

## Steady frame (still world), render CPU ms

| brushes | base p50 / p99 | M1 p50 / p99 | M2 `elements` p50 / p99 / max | M2 `multidraw` p50 / p99 / max |
|---|---|---|---|---|
| 140 | 10.21 / 17.27 | 4.29 / 6.38 | 2.10 / 3.29 / 3.48 | 2.03 / 3.23 / 3.97 |
| 572 | 45.91 / 55.74 | 15.05 / 24.60 | 3.67 / 4.54 / 5.31 | 4.00 / 6.24 / 61.50 ¹ |
| 2,300 | 180.97 / 255.50 | 56.45 / 91.60 | 9.61 / 12.97 / 15.11 | 10.12 / 13.19 / 17.48 |

¹ one isolated 61.5 ms frame; p99 6.24 ms.

`geo_batching=off` (the M1 path inside M2) matches M1 within noise at every
size, and is kept as the comparison baseline.

## GL work per steady frame (572 brushes, editor)

| | M1 | M2 `elements` | M2 `multidraw` |
|---|---|---|---|
| total GL calls | 12,236 | 349 | 297 |
| through PyOpenGL / raw | 6,382 / 5,854 | 301 / 48 | 249 / 48 |
| draw calls (all passes) | 3,345 | 58 | 58 |
| texture / VAO binds | 38 / 2,908 | 39 / 58 | 39 / 58 |
| uniform uploads | 5,838 | 33 | 33 |
| Python calls | 6,818 | 3,271 | 3,166 |

Batching choice on two GL 3.3 drivers (576 brushes, convex half only, CPU ms):
llvmpipe M1 15.6, indexed 2.1, multi-draw 2.1–2.2, draw loop 5.5; softpipe
M1 51, indexed 47, multi-draw 55 (slower than M1), draw loop 56. Neither is
assumed on other drivers: `[Renderer] geo_batching = elements | multidraw |
off`.

## Editing (`elements`): render CPU p50, per operation

One edit every other frame, deterministic script. Table sync (logic-thread
work in the app) is measured separately and excluded here.

| op | 140 | 572 | 2,300 | uploads per edit (2,300) |
|---|---|---|---|---|
| still | 2.10 | 3.67 | 9.61 | 0 |
| move (mover) | 2.16 | 3.72 | 9.92 | transform 112 B |
| reshape | 3.18 | 4.96 | 10.46 | geometry 2.9 KB, indices 31 KB |
| retexture | 2.91 | 4.49 | 11.15 | geometry 2.3 KB, indices 19 KB |
| create | 3.04 | 4.76 | 11.32 | geometry 2.8 KB, indices 18 KB |
| delete | 2.53 | 4.23 | 10.52 | indices 17 KB |
| hide / unhide | 2.16 / 2.07 | 4.01 / 3.77 | 10.25 / 9.60 | indices only |
| undo (whole world restored) | 4.39 | 10.47 | 34.12 | 0 (0 meshes rebuilt) |

A one-brush edit adds ~0.3–1.7 ms over a still frame at every world size and
uploads one mesh, one transform slot and the index lists of the (texture,
page) groups it touches; index bytes grow with the groups (bounded by the
65,536-vertex page), not with the world.

Undo, before and after: M1 renders an undo in 261 ms (572) / 890 ms (2,300),
rebuilding every convex mesh; M2 in 10.5 / 34 ms with no rebuilds, because
meshes are keyed by authored surface rather than the per-dict epoch. The
table side of undo is unchanged and still large: 300–410 ms (572) and
1.3–1.5 s (2,300) on the logic thread, mostly `get_convex` re-deriving the
windings of every restored brush dict. That is a separate change.

## Mesh cache key

Convex meshes are keyed by `GeometryRecord.surface`: every plane as authored,
each face's natural-scale flag, and the record's anchor (the AABB the mesh is
expressed in — taken from `pos`/`size` when the geometry is resolved, kept
while the surface is unchanged). Equal keys build byte-identical meshes
whatever the brush's position; any change to the mesh's input changes the key
(`tests/renderer/test_geo_mesh_key.py`). A `pos`/`size`-only change to an
existing brush is a transform. An undo of a one-brush move rebuilds exactly
that brush (was: every convex brush).

## Storage

End of each 400-frame edit run (including undos): pages 2 / 4 / 6, capacity
8,192 / 32,768 / 131,072 vertices for 6,744 / 25,896 / 102,408 live
(78–82 % used); peak capacity equal to final capacity. A 300-frame
create/resize/delete churn stays within one 4,096-vertex page.

## Correctness

Two independent oracles: the table rebuild-diff (what Debug Tables shows),
and a GPU-state draw oracle that reads back every drawn triangle's vertex
bytes, texture, UV state, transform (from the buffer texture, via GL state)
and cull face, and compares them with a frozen copy of the old path run on
the brush dicts. Parity holds in every batching mode, editor and play,
culled and not, through an editing session (reshape, transform-only move,
hide, unhide, reorder, undo, redo, delete and reuse, mode switches); faults
injected after the table (handle arrays, face columns, shared pages, the
transform buffer) fail the draw oracle only.

Full suite (`5b26ede` pending; `0564458`): 6,579 passed, 135 skipped,
5 failed — the same five as the untouched base (play-cycle leak, editor
chrome, three portal-fade), none new.

## Open for ARM / Adreno

* Not measured on hardware. Indexed versus multi-draw, buffer-texture
  fetches in the vertex shader (7 per vertex) and 16-bit index uploads on
  edit all need measuring on the target before choosing a default there.
* Per-edit index uploads (tens of KB at 2,300 brushes) are bounded but not
  free on a unified-memory GPU; `multidraw` uploads none.
* Undo's table-side cost (above) affects every renderer and remains.
* Brush model: `pos`/`size` mean different things for box and convex
  brushes, and convex face material is stored twice. M2 is correct under
  both readings; unifying them is proposed separately
  (`docs/brush-model-proposal.md`).
