> Applies to Fio 3.1.0.0611 (CANARY).

Fio's [portals](https://github.com/ViciousSquid/Fio/wiki/Portal) are drawn with **stencil apertures**: the scene beyond a portal is rendered again from a transformed camera, clipped to the portal's opening by the stencil buffer. There is no BSP, no PVS and no pre-computation; a portal is an entity that can be moved, linked and switched at any time.

## The pieces

| Module | Role |
| --- | --- |
| `engine/portal_transform.py` | The one spatial transform, GL-free: a portal's frame (`basis_from_rotation` -> right, up, normal) and the mapping of points and directions from one portal to its partner (`map_point`, `map_direction`). Rendering and gameplay both use it. |
| `engine/logic_portals.py` | `LogicPortals` on the logic thread: link topology cache, fade state, player transit and projectile transit. |
| `engine/entity_table.py` | The published portal columns: `portal_slots`, `portal_active`, `portal_basis`, `portal_width_height`, `portal_direction`, `portal_target_slot`, `portal_color`, `portal_show_rim`, `portal_fade`, `portal_glasses`. |
| `engine/renderer/forward/portals.py` | `PortalsMixin` of the forward renderer: aperture masks, stencil levels, depth flattening, the virtual scene. |
| `editor/portal_links.py` | Link groups and their shared active state. |

The renderer reads only the published table, never the Portal entities.

## Drawing one portal

For each visible, active portal with a partner (`draw_portals`, at most `MAX_PORTALS = 4` apertures a frame, counting each drawn side):

1. **Screen rectangle.** The aperture's corners are projected; a portal with no area on screen is skipped (`_portal_screen_rect`).
2. **Mask.** The aperture quad is written to the stencil buffer at the next stencil level (`portal_mask` shaders).
3. **Depth flatten.** Depth inside the mask is reset so the virtual scene is not rejected by the wall the portal sits on (`_portal_flatten_depth`).
4. **Virtual scene.** The camera is mapped through the pair by the portal transform and the scene is drawn again, restricted to the masked pixels (`_draw_portal_scene`), with culling set up for the virtual view.
5. **Rim.** The coloured rim is drawn when `show_rim` is set (`portal_rim` shaders).
6. **Fade.** While a portal is fading, what was behind it is captured first and blended back by `portal_fade` (`_capture_behind_portal`, `_draw_behind_portal`).

`portal_direction` decides which side(s) of the plane are apertures: forward, reverse or both.

## Details that matter

- **Near-plane straddle.** Within `PORTAL_NEAR_STRADDLE = 24` units of the portal plane the mask is drawn across the screen instead of as world geometry, so the near plane cannot clip it and show the wall during the last step before transit.
- **Recursion.** `MAX_PORTAL_RECURSION = 1`: a portal seen through a portal is not drawn again. Higher depths are wired (`_draw_nested_portals`) at the cost of a full scene pass per level.
- **Occlusion culling** results belong to the main view; a virtual view is a different camera.
- **Android.** The player does not implement portal virtual views ([Fio Player (Android)](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android))).

## Transit

`LogicPortals` moves the player when they cross an active portal's plane inside its aperture: position and view direction are mapped by the same transform the renderer used, so the view is continuous. A cooldown of 0.5 s (`_PORTAL_TRANSIT_COOLDOWN`) prevents bouncing straight back, and the player is placed a small epsilon past the exit plane. Projectiles transit too. The portal that was entered fires `OnPlayerEnter` / `OnTeleport`. A transit needs both ends active, which is why linked portals share one state.

Tests: `tests/renderer/`, `tests/visual/`; example `maps/Portal_Test.json`.
