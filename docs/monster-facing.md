# Monster facing and directional sprites

All built-in monsters now have eight views for idle and attack: human,
human/variant1 (zombie), flying (red demon), and flying/variant1 (blue demon).
The PNGs are in `assets/sprites/monsters/` and included in `assets/sprites.fiopak`.

The Monster's **Angle** property sets its starting heading, in degrees:
0 faces +Z, 90 faces +X, 180 faces -Z and 270 faces -X. Chasing and attacking
face the target, including at close range; patrol and noise investigation
face their destination. A stopped or sleeping monster retains its heading.
Deathmatch bots and LAN players use their actor's aim heading.
The public angle property is included in existing map/save serialization.

The camera selects the view independently in each viewport, including portal
views and the standalone GLES player. Turning the camera never changes AI state.
Each 45-degree sector selects one file:

| Rotation | View | Idle | Attack |
| --- | --- | --- | --- |
| 1 | Front | idle_1.png | shoot_1.png |
| 2 | Front-left | idle_2.png | shoot_2.png |
| 3 | Left | idle_3.png | shoot_3.png |
| 4 | Back-left | idle_4.png | shoot_4.png |
| 5 | Back | idle_5.png | shoot_5.png |
| 6 | Back-right | idle_6.png | shoot_6.png |
| 7 | Right | idle_7.png | shoot_7.png |
| 8 | Front-right | idle_8.png | shoot_8.png |

Corpses keep the existing single `dead.png` (rotation 0 in the reference).
The existing unsuffixed front images remain the editor icons and fallbacks.
Custom sprites still take priority. For example, a custom `guard.png` can have
siblings `guard_1.png` through `guard_8.png`; missing siblings use `guard.png`.
Missing built-in directional frames similarly fall back to the corresponding
single-view state and then idle. Variants preserve their own art before
falling back to the base monster. Recipe caches include the view index.

The 64 directional PNGs were generated from the four existing monster images
and extracted from an eight-view atlas. Humanoid frames share a transparent
256x384 canvas and flying frames a 256x256 canvas. Idle and attack frames use
the same scale within each appearance. To rebuild the shipped archive after
editing assets, run `python tools/pack_assets.py sprites`.
