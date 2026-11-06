# Playerstart editor model

Playerstarts use a private acrylic sign and human silhouette in the 3D view,
with its wireframe, name, Primary label and heading arrow in the 2D views.
Right-click **PlayerStart** still creates a `PlayerStart`, with the same spawn
selection, I/O, undo and map format.

The model matches the standing player's **50 × 100 × 50** collision bounds.
Both use a centre origin: the feet are exactly `spawn.pos[1] - 50`, and the
top is `spawn.pos[1] + 50`. Heading changes preserve the world footprint of
the non-rotating collision hull. In front/side views the clicked height is the
feet; top-view placement uses the Y=0 reference plane, placing the centre at
Y=50. Existing spawns retain their saved positions.

The sign is editor presentation, hidden during normal Play and excluded from
shadow casters and physical collision. Its path, rotation and scale are not
authored prop properties or map asset references. Files live in `assets/editor/`,
outside the Asset Browser's models, textures and sprites roots. They must never
be moved to a browsable asset folder or added to the model presets.

Regenerate `assets/editor/playerstart.obj` with
`python tools/build_playerstart_model.py`. The generator uses `PLAYER_HULL_SIZE`,
the same dimensions as the runtime player. OBJ material opacity provides the
clear acrylic while the silhouette and edges remain opaque.
