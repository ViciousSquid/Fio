# EntityTable

## Overview

EntityTable is Fio's dense entity projection and the other half of the renderer's T3 data boundary.

RenderTable performs this job for brushes. EntityTable performs it for entities.

The authored entity model remains object-oriented: entities such as Props, Monsters, Pickups, Lights and Portals are still represented by their normal authoring/runtime objects.

The renderer does not need to repeatedly inspect those objects to decide how they should be rendered.

Instead, EntityTable resolves the relevant information into dense NumPy columns addressed by integer slots.

> The authoring model is object-oriented. The execution model is data-oriented.

## Why EntityTable Exists

The old entity render path required repeated Python inspection of every entity.

At frame time this included operations such as:

- isinstance chains
- property lookups
- string normalisation
- render-kind classification
- position publication
- sprite/model decisions
- light collection

Those operations were repeated even when the underlying classification had not changed.

At a measured 961 entities, the old paths were approximately:

- 0.65 ms for the position/light/visibility publication work
- 0.81 ms for the entity render-kind classification work

The problem was not that entity semantics are inherently object-oriented.

The problem was that static semantic decisions were being rediscovered in the frame loop.

EntityTable moves those decisions to a projection boundary.

## What It Is and Is Not

Like RenderTable, EntityTable is a derived execution representation.

It:

- stores no independent authored state
- resolves columns from the authoritative entity objects
- keeps the entity's existing UUID as identity
- uses integer slots for dense frame-time addressing
- is disposable and rebuildable
- is consumed numerically by renderer passes

It is not a second entity system.

It is not a replacement for the authoring objects.

It is not a generic ECS.

## Identity and Slots

Every entity retains its authoritative UUID.

The table assigns each row an integer slot.

A slot is valid only for the table's current generation.

The UUID is therefore the persistent identity; the slot is the execution address.

Frame code should operate on slot arrays:

    class_bits[visible_slots]
    pos[visible_slots]
    sprite_key_id[visible_slots]
    model_recipe_id[visible_slots]

It should not repeatedly perform:

    uuid -> Python dictionary -> entity -> property

during rendering.

## Classification Bits

The table stores a uint16 classification word per entity.

The current classification inputs include:

| Bit | Meaning |
| --- | --- |
| ENT_SKIP | PathNode/non-rendered entity |
| ENT_ALWAYS_SPRITE | Entity that must render through the sprite path |
| ENT_HAS_MODEL | Has a model path |
| ENT_MODE_MODEL | Render mode is model |
| ENT_MODE_BILLBOARD | Render mode is billboard |
| ENT_HAS_SPRITE | Has a sprite path |
| ENT_PICKUP | Pickup classification |
| ENT_ENTITY_SPRITE | Entity classes rendered as sprites |
| ENT_PROP | Prop classification |
| ENT_LIGHT | Light classification |
| ENT_MONSTER | Monster classification |
| ENT_PORTAL | Portal classification |
| ENT_SPRITE_WARM | Sprite identity may change at runtime |

These bits deliberately encode the inputs to the entity render decision rather than storing only a final verdict.

That allows frame-time classification to combine static entity facts with dynamic conditions such as play mode, show-sprites state and the live hidden flag.

## Cold, Warm and Live State

Entity state follows the same refresh discipline as brush render state.

### Cold state

Cold state changes at an edit/reconciliation boundary.

Examples include:

- class_bits
- model recipe identity
- authored sprite identity
- authored sprite size
- model transform recipe
- portal dimensions
- portal colour
- portal direction
- portal rim settings

These values are resolved when the entity row is created or explicitly refreshed.

### Warm state

The primary warm column is position.

Entities can move continuously, so pos is published into the dense table each frame.

The update is performed as a bulk NumPy-oriented store rather than writing individual NumPy elements from a Python loop.

### Live state

The live hidden flag is read every frame.

This is required because BigWorld can park entities by changing hidden state directly.

Caching that value as cold state would allow the projection to disagree with the runtime world.

## Sprite Identity

Sprite rendering has an additional dynamic case.

Some entity types can change their sprite without an editor edit.

The ENT_SPRITE_WARM classification identifies rows whose sprite identity must be re-resolved during runtime.

Examples include:

- Monster state changes
- Monster shooting/dead state
- LogicGate representation
- Pickup item representation
- Prop representation changes

The important distinction is:

- sprite position is dynamic for entities generally
- sprite size is normally cold
- sprite identity is cold unless the entity class is explicitly sprite-warm

This prevents unnecessary per-frame property interpretation for static entities.

## Model Rendering

Model entities also use dense representation.

The table stores a model recipe ID and the numerical transform information required by the model pass.

The renderer can therefore gather model rows without rediscovering:

- whether an entity has a model
- which model recipe it uses
- the base transform
- the normal transform

A missing model recipe uses a sentinel rather than requiring the renderer to inspect the original entity object.

## Lights

Lights are represented as dense rows as part of the same entity projection.

The table can expose:

- light colour
- intensity
- radius
- enabled state
- shadow-casting state

The renderer can gather light rows numerically.

Runtime light state is refreshed because I/O can toggle or retune lights without requiring the whole entity table to be structurally rebuilt.

## Portals

Portals are another entity family that benefits from dense representation.

The table stores numerical portal state such as:

- target slot
- active state
- direction code
- width/height
- basis/orientation
- fade
- colour
- rim visibility

Portal targets are resolved from authored names to integer slots at the cache boundary.

This means portal rendering does not need to perform name-based entity lookup during the hot path.

Portals and lights are also represented as cull-exempt classifications where required by the renderer.

## Numerical Render Classification

The renderer can reproduce the entity render decision using masks over the classification arrays.

Conceptually:

    entity slots
        |
        +--> class_bits
        |
        +--> live hidden state
        |
        +--> view/runtime flags
        |
        v
    model slots / sprite slots

This replaces a Python object/classification chain with array operations.

The resulting slot arrays can then be passed directly to specialized renderer paths.

## Sprite Rendering Boundary

Sprite rendering has one execution boundary:

    EntityTable
        -> sprite slots
        -> sprite key / texture IDs
        -> numerical sorting
        -> texture runs
        -> reusable instance buffer
        -> glDrawArraysInstanced

The sprite renderer does not need to touch the original entity objects.

For each frame it can:

1. gather positions and sizes from dense columns
2. resolve texture IDs
3. sort numerically
4. identify contiguous texture runs
5. gather instance data into reusable staging storage
6. issue one instanced draw per texture run

The Python work is therefore proportional primarily to the number of resulting draw runs, not to the number of Things in the world.

## Reusable Staging Buffers

The sprite path uses reusable NumPy staging storage.

Position and size columns are gathered directly into the staging buffer with np.take(..., out=...).

This matters because a data-oriented renderer can otherwise accidentally recreate the old cost in a different form:

    Python objects
        -> temporary Python list
        -> temporary NumPy array
        -> copy into GPU staging

The intended path is closer to:

    dense table
        -> reusable staging buffer
        -> OpenGL buffer upload

The goal is bulk movement of existing numerical data, not repeated object-to-array conversion.

## Packed Render Keys

Entity render classification can feed the same packed-key/run architecture used elsewhere in Fio.

The general execution model is:

    dense entity state
        -> classification
        -> packed key
        -> numerical sort
        -> equal-key runs
        -> specialized GPU submission

This makes entity rendering part of the same renderer architecture rather than a special object-oriented side path.

## Multiple Views and Split-Screen

EntityTable belongs to the shared world.

It is not duplicated per camera.

Multiple views consume the same dense entity projection while keeping view-specific state separate.

Each view can independently determine:

- visibility
- distance culling
- camera-relative ordering
- projection
- viewport
- depth-dependent sprite ordering
- view-specific render flags

This is especially important for split-screen.

A second view should not require another pass over every entity object merely to rebuild an equivalent render representation.

The shared table is the world-level execution representation; each view derives its own slot selections and ordering from it.

## BigWorld Interaction

BigWorld changes runtime visibility without changing the underlying authored entity.

That is why the table distinguishes:

- static classification
- warm transform data
- live hidden state

An entity can remain in the dense table while BigWorld parks it.

The renderer then removes it numerically through the live hidden mask rather than rebuilding the entity representation.

This preserves a stable dense address space while allowing streaming state to remain dynamic.

## Reconciliation

Structural changes cause the entity projection to reconcile.

Typical causes include:

- entity insertion
- entity deletion
- authored render-property changes
- world epoch changes
- entity replacement

The reconciliation process maintains the relationship:

    entity UUID <-> table slot

Untouched rows can retain their cold state where possible.

A generation counter identifies the lifetime of the current slot mapping.

Consumers must not assume that an old slot remains valid after a structural reconciliation.

## Correctness Invariants

Useful tests for EntityTable include:

### Projection equivalence

Rebuilding the table from the same authoritative entity list produces equivalent dense state.

### Classification equivalence

The dense classification path produces the same model/sprite decision as the authoritative entity semantics for the same runtime flags.

### Identity

Entity UUIDs remain authoritative.

Slots are never treated as persistent identity.

### Hidden-state correctness

Changing the live hidden state is reflected without requiring a cold-table rebuild.

### Dynamic sprite correctness

Sprite-warm entities update their sprite identity when their runtime state changes.

### Model correctness

Changing model-path or render-mode state updates the dense model recipe at the appropriate cache boundary.

### Light correctness

Runtime light toggles and parameter changes reach the dense light columns.

### Portal correctness

Portal target names resolve to the correct dense slots and runtime portal state remains current.

### Split-screen correctness

Multiple views can consume the same table while producing independent visibility and ordering results.

## What EntityTable Is Not

EntityTable is not:

- a replacement for Fio's entity objects
- a generic ECS
- a second source of truth
- a per-camera entity database
- a persistent serialization format
- a GPU resource manager
- a reason to eliminate object-oriented authoring

Its purpose is narrower:

**turn entity semantics into dense execution data before the renderer needs them.**

## Architectural Summary

EntityTable closes the second major object-to-render boundary in Fio.

Brushes follow:

**authoring objects → RenderTable → numerical render pipeline**

Entities follow:

**authoring objects → EntityTable → numerical render pipeline**

Together they establish a consistent rule:

> Python objects describe the world. Dense numerical projections execute the world.

The renderer therefore does not need to repeatedly ask thousands of authored objects what they are.

It consumes already-resolved data and performs the operations that are naturally numerical:

**mask → gather → key → sort → run → instance → draw.**

That is the practical meaning of Fio's T3 architecture.
