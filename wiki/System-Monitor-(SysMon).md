> Applies to Fio 3.5.0.1111.

**SysMon** is the overlay in the 3D view that shows what a frame costs. Toggle it with **F3** (`[Shortcuts] key_sysmon`); `[Display] always_show_sysmon` shows it at start-up. Module: `engine/sysmon.py`. It is switched off when a package is played in kiosk mode (F3 brings it back).

<img src="https://github.com/user-attachments/assets/7c23628b-561a-44d6-acc1-feb4114c7dcf" width="300">

| Line | Meaning | Source |
| --- | --- | --- |
| `Frame` / `FPS` | Last frame time in ms and frames per second | The view's paint timing |
| `TPS` | Logic ticks per second | `LogicThread.actual_tps` |
| `Brushes` (Visible) | Brushes in the world and how many survived culling | The published frame |
| `Culled` | Share of brushes culled | Frustum, view distance and [occlusion culling](https://github.com/ViciousSquid/Fio/wiki/Cell-Occlusion-Culling) |
| `Tris` (Visible) | Triangles in the world and visible | `RenderStats` |
| `Draws` | Draw calls issued | `RenderStats` |
| `Things` | Entity count | The world |
| `VRAM` | GPU memory used / free | A driver extension query, only where the driver advertises it; otherwise `N/A` |

Below the lines is a frame-time graph, which stays visible when the panel is collapsed. The expensive figures are recomputed once a second, and the VRAM query is cached, so the monitor does not cost a GL query per frame. Software rasterisers may report invented memory figures.

For the numbers behind the picture -- every column of the dense tables -- use [Debug Tables](https://github.com/ViciousSquid/Fio/wiki/Debug-Tables).
