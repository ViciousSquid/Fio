# Overhead (top-down) player sprite frames

The native Overhead camera mode (editor **Camera ▸ Overhead**) draws the player
as a ground sprite that faces the player's heading, animating between an idle
pose and a two-frame walk cycle. Drop the three frames here:

| File          | When shown          |
|---------------|---------------------|
| `player1.png` | idle (standing)     |
| `player2.png` | walking (frame A)   |
| `player3.png` | walking (frame B)   |

Requirements for clean playback:

- **All three the same canvas size**, with the character **registered to the
  same spot** in each (otherwise the animation jitters).
- **Transparent background** (PNG with alpha) — the sprite is alpha-cut.
- The character should be drawn facing **up** in the image (toward the top);
  the engine rotates the sprite to the player's heading. If it faces the wrong
  way, trim it with `QtGameView.overhead_sprite_facing_offset` (degrees) — no
  code change.

Renderer/tunables live in `engine/overhead_sprite.py` and `engine/qt_game_view.py`
(`overhead_sprite_size`, `overhead_walk_fps`, `overhead_sprite_facing_offset`,
`overhead_sprite_enabled`). If the frames are missing the renderer simply draws
nothing — the camera still works.
