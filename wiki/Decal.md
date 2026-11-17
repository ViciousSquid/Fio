> Applies to Fio Canary 1611 and later. Add this to the existing Decal page.

# Decal: Tint

A Decal has a **Tint** in the Property editor: a colour its image is multiplied by. White, the default, leaves the image as it is; **Reset** puts it back.

| | |
| :--- | :--- |
| Property | `tint`: `[r, g, b]`, each 0–1. Maps saved before the property existed load as white |
| An image decal | The image's colour times the tint, then painted as before (it stains the surface: mid grey leaves it alone). A grey grime decal tinted green is green grime |
| A wet decal | The tint colours the wet surface: a dark red tint makes a pool of blood of a puddle, a dark brown one oil |
| Text | A sign's words are part of its image and take the tint with it |
| Blood | Not tinted |
| I/O | `SetTint` — `r g b` (0–255 or 0–1) or `#rrggbb`. With no parameter, or one that makes no sense: white |
| Android player | The same, by the same shader |
| Cost | None. The tint rides in a row of the decal's instance data that was already uploaded and unused |
