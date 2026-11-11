> Applies to Fio 3.5.0.1111.

Split-screen puts a second local player in the running game. In Play, press **F9** or run the console command `ss` (Play only). Example: enter Play (F5), F12 for full screen, then F9.

- Player 2 spawns 32 units beside the last Player Start, facing the same way (`QtGameView._toggle_splitscreen`), and is handed to the logic thread as `player_runtime.player2`. Toggling off removes them.
- Player 2 is controlled with the cursor arrow keys or an attached gamepad (through pygame). With no gamepad the editor logs "No gamepad connected - using arrow keys for P2".
- Turn sensitivity: Settings > Split-Screen, stored as `[Controls] p2_turn_sensitivity` (30-500 degrees/s).

## Limits

- Collectables are shared.
- Monsters target player 1 only.
- The players do not see each other.
- Damage indicators are drawn for player 1 only, over the whole view.

_Thanks to StarlitWolf for suggesting this feature._
