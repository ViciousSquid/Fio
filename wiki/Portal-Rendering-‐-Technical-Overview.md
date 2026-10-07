# World Portals

Fio implements **real-time non-Euclidean world portals** connecting arbitrary locations and orientations within a level.

A portal is a live connection between two parts of the world. It does not depend on a pre-generated portal texture, BSP visibility compilation, VIS/PVS data, or an offline visibility pass. When a portal is visible, Fio transforms the current camera through the portal pair and renders the destination world directly through the portal aperture.

The portal system combines:

* **Stencil-buffer masking** to define the visible portal aperture
* **Portal-space camera transformation** to generate the destination view
* **Oblique near-plane clipping** to clip the destination scene against the exit portal
* **Bounded recursive rendering** for portals visible through other portals
* **Runtime traversal** using the same spatial relationship used by rendering

This makes the portal part of the live world rather than a special-case visual effect.

---

## Portal Pairing

A portal consists of an aperture and a corresponding destination portal.

The two portals define a spatial transformation between their coordinate frames. This transformation is used both when rendering through the portal and when moving an object through it.

Conceptually:

```text
Source Portal A
      │
      │ portal-space transformation
      ▼
Destination Portal B
      │
      ▼
Transformed camera / object
```

The destination portal's position and orientation establish the coordinate frame of the resulting view.

Consequently, portals are not simply teleport points. Their relative transforms determine both:

1. what the viewer sees through the portal; and
2. how an object crossing the portal is transformed into the destination space.

A portal can therefore connect arbitrary surfaces and orientations within the same world.

---

## In the Editor

Portals never display an editor sprite (there is no `portal.png`). The editor shows a portal by its **aperture wireframe**, drawn directly from the portal's EntityTable row.

---

## Stencil Aperture

Portal visibility begins by defining the portal's screen-space aperture.

Fio uses dedicated portal-mask rendering to establish the region through which the destination scene may be displayed.

The portal mask resources include:

```text
portal_mask.vert
portal_mask.frag
```

The mask is written to the stencil buffer. Subsequent destination-scene rendering is restricted to the pixels belonging to the portal aperture.

Conceptually:

```text
Normal framebuffer
┌──────────────────────────────┐
│                              │
│       ┌────────────┐         │
│       │   PORTAL   │         │
│       │   APERTURE │         │
│       └────────────┘         │
│                              │
└──────────────────────────────┘
```

Only the pixels covered by the aperture are eligible to receive the portal destination view.

This is fundamentally different from rendering the destination world into a texture and placing that texture on the portal surface.

The destination world is rendered **into the existing view at the time the portal is processed**.

---

## Virtual Camera Transformation

Once the portal aperture has been established, Fio generates a virtual camera representing the viewer from the destination portal's coordinate frame.

For a source portal `A` and destination portal `B`, the conceptual transformation is:

```text
destination_camera =
    B · inverse(A) · source_camera
```

The implementation operates on the portal coordinate frames rather than treating the portal as a simple positional offset.

This preserves the viewer's relative position and orientation when crossing the portal transform.

For example:

```text
Viewer
  │
  │ looking through A
  ▼
Portal A
  │
  │ transform A → B
  ▼
Portal B
  │
  ▼
Virtual destination camera
```

The same portal relationship is also used by traversal logic, allowing rendering and physical portal crossing to share the same spatial definition.

---

## Oblique Near-Plane Clipping

A transformed camera alone is insufficient.

If the destination scene were rendered using an ordinary projection matrix, geometry on the wrong side of the destination portal could appear through the aperture.

Fio therefore uses **oblique near-plane clipping** for the portal view.

The destination portal plane becomes the effective clipping plane for the virtual camera:

```text
                 destination world
                       │
                       │
              ─────────┼─────────
              exit portal plane
                 clipping plane
                       │
                       │
                  virtual camera
```

The portal projection is modified so that geometry crossing the destination portal plane is clipped correctly.

This prevents surfaces behind or in front of the exit portal from leaking into the portal view.

The result is that the virtual scene behaves as though the viewer is looking outward from the destination portal itself.

---

## Portal Scene Pass

Portal rendering uses a dedicated portal-scene rendering state.

The renderer tracks this through:

```python
_portal_scene_pass
```

The state identifies rendering performed for a transformed portal destination rather than the primary world view.

This allows portal-specific rendering behaviour to be applied while the destination scene is being generated without permanently changing the normal scene-rendering state.

The destination view uses the same live world data as the surrounding scene.

There is no separately constructed copy of the destination environment.

---

## Live World Rendering

A portal does not contain a baked representation of its destination.

When a portal is visible, Fio renders the destination using the current world state.

This means that dynamic world state remains visible through portals, including changes to the scene that occur during runtime.

Conceptually:

```text
                    Same live world
                         │
             ┌───────────┴───────────┐
             │                       │
        Primary view            Portal view
             │                       │
             ▼                       ▼
       Main camera             Transformed camera
```

The portal is therefore a second view into the world rather than a textured representation of another location.

This is particularly important for Fio's editor/runtime model: portal views participate in the same live rendering and simulation environment rather than requiring a separate portal-baking pipeline.

---

## Portal Recursion

A destination view may itself contain visible portals.

Fio supports bounded recursive portal rendering through:

```python
MAX_PORTAL_RECURSION = 1
```

The default configuration therefore permits one additional portal-rendering level.

Conceptually:

```text
Main View
   │
   └── Portal A
         │
         └── Destination View
               │
               └── Portal B
                     │
                     └── Recursive Destination View
```

The recursion limit provides an explicit bound on additional rendering work.

Increasing recursion depth can produce effects such as:

```text
Portal
  ↓
Portal
  ↓
Portal
  ↓
Portal
```

but every additional level represents another transformed scene view and therefore another rendering cost.

The recursion limit prevents this cost from becoming unbounded.

---

## Portal Count Limit

Fio also bounds the number of portal apertures processed for a frame:

```python
MAX_PORTALS = 4
```

This limits the number of portal destination views that can be generated during a frame.

It does **not** limit the number of portal entities that may exist in a level.

A map may therefore contain more than four portals. The limit concerns the amount of portal rendering work performed for an individual frame.

Conceptually:

```text
World
 ├── Portal A ── rendered
 ├── Portal B ── rendered
 ├── Portal C ── rendered
 ├── Portal D ── rendered
 ├── Portal E ── not processed this frame
 └── Portal F ── not processed this frame
```

The exact set processed depends on portal visibility and the renderer's portal-selection path.

---

## Near-Straddle Handling

A special case occurs when the camera approaches or straddles a portal plane.

At very close distances, the camera's ordinary near clipping plane can intersect the portal aperture geometry itself. This can cause the portal mask to disappear or become incorrectly clipped.

Fio provides a dedicated threshold:

```python
PORTAL_NEAR_STRADDLE = 24.0
```

Within this distance, the portal mask can use special handling rather than relying exclusively on its ordinary world-space aperture geometry.

The purpose is to keep the portal aperture stable when the camera is extremely close to or crossing the portal plane.

Setting:

```python
PORTAL_NEAR_STRADDLE = 0
```

disables this special handling.

This is a rendering safeguard; it does not alter the underlying portal transform or traversal relationship.

---

## Portal Visibility Distance

Portal destination rendering is also subject to a configurable visibility-distance limit.

A portal may continue to exist and remain usable while its additional destination scene is no longer rendered once it falls outside the configured portal-view range.

This separates two different concepts:

```text
Portal existence
      │
      ├── gameplay / traversal
      │
      └── visual destination rendering
```

The portal can therefore remain part of the world without forcing the renderer to generate an additional scene view at arbitrary distances.

This is particularly important on Fio's low-power target hardware, where avoiding unnecessary secondary scene passes is a deliberate part of the renderer design.

---

## Portal Traversal

Portal rendering and portal traversal use the same underlying portal relationship.

When an object crosses a portal, its spatial state is transformed from the source portal's coordinate frame into the destination portal's coordinate frame.

Conceptually:

```text
Source space
    │
    │ portal transform
    ▼
Destination space
```

The transformation applies to the object's spatial state rather than simply replacing its position with the destination portal's position.

This allows orientation and movement through a portal to remain consistent with the portal pair's relative orientation.

The portal therefore acts as a genuine spatial connection in the world rather than merely triggering a teleport event.

---

## Portal Shaders and Resources

Portal-specific shader resources are maintained separately from the normal material shaders.

The portal mask shaders are:

```text
portal_mask.vert
portal_mask.frag
```

Portal-specific geometry and rendering resources are also maintained for the aperture and portal-facing operations required by the renderer.

The destination world itself continues to use the normal scene-rendering pipeline.

The portal system therefore adds a transformed rendering pass rather than introducing a second material/rendering representation of the destination world.

---

## Rendering Sequence

For a visible portal, the rendering process can be summarized as:

```text
1. Identify a portal to process
        │
        ▼
2. Establish the portal aperture
        │
        ▼
3. Write the stencil mask
        │
        ▼
4. Transform the camera through the portal pair
        │
        ▼
5. Construct the portal clipping plane
        │
        ▼
6. Enter portal scene pass
        │
        ▼
7. Render the live destination scene
        │
        ├── render visible portals recursively if permitted
        │
        ▼
8. Restrict the destination result to the stencil aperture
        │
        ▼
9. Restore normal scene-rendering state
```

The important property is that the destination view is generated **at render time**.

There is no requirement for:

* a pre-rendered portal texture;
* a BSP portal database;
* VIS/PVS compilation;
* offline portal visibility calculation;
* a portal lightmap;
* a baked destination view.

Portal visibility is therefore a runtime rendering concern.

---

## Performance Characteristics

A portal effectively introduces an additional camera view into the frame.

Its rendering cost is therefore primarily determined by:

* the number of portal apertures processed;
* the amount of visible destination geometry;
* the configured recursion depth;
* the screen-space area of the portal views;
* the normal cost of the destination scene.

Fio explicitly bounds the two multiplicative components of portal rendering:

| Limit                  |  Value | Purpose                                             |
| ---------------------- | -----: | --------------------------------------------------- |
| `MAX_PORTALS`          |    `4` | Maximum portal apertures processed for a frame      |
| `MAX_PORTAL_RECURSION` |    `1` | Maximum additional recursive portal-view level      |
| `PORTAL_NEAR_STRADDLE` | `24.0` | Threshold for close/straddling portal-mask handling |

The number of portal **entities** in a level is therefore not equivalent to the number of additional scene renders.

Only portals selected for the current frame contribute portal-view rendering work.

---

## Architectural Characteristics

Fio's portal implementation deliberately avoids an offline visibility architecture.

Traditional portal/BSP rendering approaches can use precomputed relationships between regions of a level to determine visibility before runtime.

Fio instead treats the portal as a live spatial and rendering primitive:

```text
Portal
 ├── spatial relationship
 │     └── traversal
 │
 └── rendering relationship
       ├── aperture
       ├── transformed camera
       ├── clipping plane
       └── live destination scene
```

This fits Fio's broader **edit-and-play** architecture.

Changing the world does not require rebuilding a portal visibility database before the new configuration can be viewed. The portal relationship is evaluated directly against the current world state.

---

## Source Implementation

The portal system spans several engine and editor subsystems.

Primary implementation areas include:

* `editor/things.py` — `Portal` entity definition
* `engine/entity_table.py` — dense portal state (topology, aperture, active/fade, colour, rim)
* `engine/renderer/core/visibility.py` — portal camera maths: virtual views, oblique clipping, apertures
* `engine/renderer/core/overlays.py` — the portal aperture wireframe the editor shows
* `engine/renderer/forward/portals.py` — `ForwardRenderer`'s stencil portals: limits, aperture masks, recursion, the portal virtual scene and its GL resources
* `engine/shaders.py` — portal shader resources
* `engine/portal_transform.py` — the portal frame/link transform shared by rendering and gameplay
* `editor/io_system.py` — portal-related I/O registration
* `editor/io_handlers.py` — portal activation and I/O handling
* `engine/logic_thread.py` / `engine/logic_portals.py` — runtime portal traversal

Portal rendering as described here is the built-in `ForwardRenderer`'s technique. Since Fio 3.0 renderers are swappable (see the [Renderer Technical overview](https://github.com/ViciousSquid/Fio/wiki/Renderer-Technical-overview)); the camera maths in `core` is available to any renderer, but how portals are composited is each renderer's choice.

The system therefore spans:

```text
Editor
  │
  ├── Portal entity
  │
  └── I/O
        │
        ▼
Runtime
  │
  ├── Portal traversal
  │
  └── Renderer
        │
        ├── Aperture / stencil
        ├── Camera transformation
        ├── Oblique clipping
        ├── Destination scene
        └── Bounded recursion
```

The result is a **live, runtime-evaluated non-Euclidean connection** between two locations in the world, using the same portal relationship for both visual projection and spatial traversal.
