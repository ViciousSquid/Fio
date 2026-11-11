> Applies to Fio 3.5.0.1111.

**Tools > Autocaulk** applies the `nodraw.jpg` texture to every box-brush face that can never be seen, so those faces are not drawn. One undo step restores them. Implementation: `MainWindow.autocaulk` in `editor/main_window.py`.

<img src="https://github.com/user-attachments/assets/66298074-beee-4a41-b21f-5ff3f1ee2419" width="400">

## What it does

- Considers solid additive brushes: subtract brushes, triggers and fog volumes are left out.
- For each of a box brush's six faces, tests whether the face is covered by other brushes (`_is_face_occluded`); a covered face gets `nodraw.jpg`. Faces already set are skipped.
- **Angled (convex) brushes are never caulked**: the test samples a brush's axis-aligned faces, which an angled brush does not have.
- Reports how many faces were caulked.

## Why

A room made of six brushes has faces no player can see: the outsides of the walls, the top of the ceiling brush, the underside of the floor brush. A face textured `nodraw` is skipped by the renderer (and by the Android player, which does not load its texture). You can also apply nodraw by hand from the [Asset Browser](https://github.com/ViciousSquid/Fio/wiki/Asset-Browser). `[Display] show_caulk` controls whether caulked faces are shown in the editor.
