# RenderTable

## Overview

RenderTable is Fio's dense render projection of the authored brush world.

It is one half of the renderer's T3 data boundary: the authored world remains object- and dictionary-oriented, while the renderer consumes a compact numerical representation built from that world.

A RenderTable contains one row per brush and stores render-relevant state in dense NumPy columns addressed by an integer **slot**.

The table is a **derived execution representation**, not a second world.

> The authoring model is object-oriented. The execution model is data-oriented.

## Why RenderTable Exists

Before the dense render projection, Fio could cull brushes numerically and then immediately fall back to the authored brush dictionaries to determine what each visible brush actually was.

That meant the renderer repeatedly performed work such as:

- Python dictionary lookups
- shader/material classification
- string comparisons
- texture-name resolution
- UV-property resolution
- geometry classification
- dynamic-object checks

Much of that information changes only when a brush is edited.

RenderTable moves those stable decisions to a cache boundary. Frame-time rendering can then operate on integer slots, bit fields and numerical columns.

The important distinction is:

- **Authoring state:** the brush dictionaries owned by the world.
- **Execution state:** the numerical projection in RenderTable.
- **GPU state:** OpenGL resources maintained by the renderer.

The table does not replace the authored brush.

## Core Invariants

### No authored source of truth

RenderTable stores resolutions of authored data.

Deleting the table and rebuilding it from the authoritative brush list must reproduce the same render representation.

The table is not edited as a second representation of the world.

### Stable identity, temporary slots

A brush keeps its existing UUID as its identity.

A slot is only a dense numerical address valid for the table's current generation.

Frame code therefore operates on dense slot arrays rather than repeatedly translating a UUID through a Python dictionary.

The UUID remains the world identity; the slot is an execution address.

### GL-free projection

The table does not require an OpenGL context.

Texture names are interned to dense integer IDs. The renderer maps those IDs to actual OpenGL texture handles on the rendering thread.

This keeps the projection usable by headless tests and logic-side code without coupling it to GL lifetime.

## Dense Columns

The exact column set evolves with the renderer, but the principal groups are:

| Column | Purpose |
| --- | --- |
| center | Brush centre / translation |
| half | AABB half-extents |
| rot | Rotation axis and angle |
| class_bits | Numerical render/material classification |
| tex_name_id | Dense texture-name IDs for the six cube faces |
| uv_scale | Per-face UV scale |
| uv_angle | Per-face UV rotation |
| uv_shift | Per-face UV offset |
| uv_natural | Natural-scale flags |
| uv_has_scale | Explicit UV-scale flags |
| colour | Resolved brush colour |
| glow_colour | Resolved glow colour |
| geo_epoch | Geometry-cache version |
| geometry_id | Dense geometry handle |
| special-material columns | Water, glass and fog parameters |

The representation is intentionally numerical so consumers can gather rows with NumPy rather than materialising brush objects.

## Classification Bits

class_bits is a uint16 classification word.

Current brush classifications include:

- authored-hidden
- water
- fog
- glass
- glow
- trigger
- subtract
- custom/convex geometry
- textured
- shadow-caster
- dynamic mover/door
- texture tiling

These bits replace repeated semantic discovery in hot renderer paths.

For example, whether a brush is water is resolved when its cold render state is rebuilt rather than rediscovered from strings for every visible brush on every frame.

## Cold, Warm and Live State

The table deliberately separates state by how frequently it changes.

### Cold state

Cold state is expensive to derive and changes only at editor/cache boundaries.

Examples:

- class_bits
- texture-name IDs
- UV state
- material parameters
- geometry classification
- geometry epoch

Cold state is rebuilt when the relevant world epoch or dirty-object boundary changes.

### Warm state

Warm state is cheap to refresh because transforms can change continuously.

Examples:

- center
- half
- rot

Movers and doors are the main reason these columns are refreshed during normal execution.

### Live state

Some state cannot safely be cached.

The most important example is the live hidden flag.

BigWorld can park an object by changing its hidden state directly. A cached copy could therefore become stale without a structural world change.

The renderer reads the live hidden state at frame time.

This is deliberate: not everything should be forced into a cold cache.

## Geometry

Convex/custom brush geometry is represented by dense geometry handles and records rather than forcing the renderer to rediscover geometry from the authored brush during every draw.

Ordinary box brushes can use the shared cube geometry.

Brushes with their own convex geometry receive a geometry record containing the information required to prepare and draw that mesh.

The geometry epoch provides a numerical way for consumers to determine whether cached geometry is stale.

## Textures

Texture names are interned into dense integer IDs.

The table pre-interns the common sentinel names:

- default.png
- caulk.jpg
- nodraw.jpg

A texture ID is therefore suitable for numerical classification and gathering.

The renderer performs the separate mapping:

**texture-name ID → OpenGL texture ID**

That separation is important because the logic-side table should not own GPU resources.

## Rendering Pipeline

The normal conceptual flow is:

    Authoritative brush dictionaries
              |
              v
          RenderTable
              |
              +--> numerical visibility/classification
              |
              +--> packed render keys
              |
              +--> sorted equal-key runs
              |
              v
         renderer passes
              |
              v
          OpenGL draws

The renderer should not have to ask a brush dictionary what it is after the projection has already resolved that information.

## Relationship to Render Keys

RenderTable provides dense render state.

render_keys.py then turns relevant state into packed sortable integer keys.

The renderer can sort those keys numerically and identify contiguous runs with equal state.

Conceptually:

    dense state
       -> packed key
       -> numeric sort
       -> contiguous runs
       -> GPU submissions

This is the bridge between classification and batching.

The goal is not to eliminate all Python loops. Small loops over already-formed draw runs are acceptable.

The goal is to eliminate Python work proportional to the number of world objects when the same work can be expressed as bulk numerical transformation.

## Frame-Time Principle

The hot path should operate on:

- NumPy arrays
- integer slots
- classification masks
- packed keys
- contiguous runs
- reusable staging buffers

It should not repeatedly perform:

- object type discovery
- dictionary traversal
- string classification
- object-to-array conversion
- allocation of temporary arrays for every draw
- Python iteration over every visible brush

This is the practical form of Fio's dense numerical core.

## Multiple Views and Split-Screen

RenderTable belongs to the world, not to an individual camera.

Multiple views therefore consume the same dense brush projection.

Each view owns its own:

- camera
- projection matrix
- view matrix
- viewport
- visibility/culling result
- depth ordering where required
- view-specific render state

The world projection is shared; view-dependent decisions are not.

This is essential for split-screen rendering because creating one object representation per view would recreate the very duplication the dense projection is intended to remove.

## Lifetime and Reconciliation

A table has a generation.

A structural reconciliation can change the slot mapping and increments the generation.

Consumers that retain slots must therefore treat slots as generation-scoped addresses.

The authoritative UUID remains valid across these cache-boundary changes.

A normal frame should not rebuild cold state unnecessarily.

The intended lifecycle is:

1. Authoring/world state changes.
2. The relevant epoch or dirty boundary changes.
3. RenderTable reconciles.
4. Cold columns are resolved.
5. Dynamic transforms are refreshed.
6. The renderer consumes dense rows.

## Testing and Correctness

A useful invariant for tests is:

> Rebuilding RenderTable from the same authoritative brush state must produce the same render classification and material representation.

Tests should cover:

- empty worlds
- one-brush worlds
- mixed material classes
- water/fog/glass/glow/trigger brushes
- subtract brushes
- movers and doors
- custom convex geometry
- texture changes
- UV changes
- hidden/visible transitions
- geometry invalidation
- row insertion/removal
- slot remapping
- generation changes
- multiple render views
- split-screen
- headless construction without an OpenGL context

## What RenderTable Is Not

RenderTable is not:

- a replacement for the authored world
- a second editable scene graph
- a GPU resource manager
- an OpenGL object database
- a per-camera copy of the world
- a requirement to convert every Fio object into a Python wrapper
- a general-purpose ECS

It is a narrow execution projection designed around the renderer's actual data requirements.

## Architectural Summary

RenderTable is the point where Fio stops asking the renderer to repeatedly interpret authored brush objects.

The world remains authoritative and human-editable.

The renderer receives a dense numerical projection.

From there, Fio can perform:

**cull → classify → key → sort → run → draw**

without repeatedly rediscovering the meaning of every brush.

That is the purpose of T3: not to replace Fio's object-oriented authoring model, but to prevent that model from becoming the CPU's frame-time execution model.
