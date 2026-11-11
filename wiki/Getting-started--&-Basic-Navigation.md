> Applies to Fio 3.5.0.1111.

Fio is a world editor and the engine that runs the world: you build with brushes and entities, press **Play**, and the world you are editing runs. To run from source see [Building and Running from Source](https://github.com/ViciousSquid/Fio/wiki/Building-and-Running-from-Source).

<img src="https://github.com/user-attachments/assets/addd994b-0f55-4b68-b3d5-b8a61fc63681" width="700">

## The window

- **3D view**: the world as it will be played. **Z** toggles freelook (or hold the right mouse button); **W A S D** move; **Esc** releases the mouse.
- **2D view**: Top, Front or Side; **Ctrl+Tab** cycles them. Drag here to draw and move brushes; right-click to place entities.
- **Side panel**: the [Property editor](https://github.com/ViciousSquid/Fio/wiki/Property-editor), the [Asset Browser](https://github.com/ViciousSquid/Fio/wiki/Asset-Browser), the [Debug console](https://github.com/ViciousSquid/Fio/wiki/Debug-console).
- **[Scene Hierarchy](https://github.com/ViciousSquid/Fio/wiki/Scene-Hierarchy)**: everything in the level.

<img src="https://github.com/user-attachments/assets/1c98a6a8-731e-440c-a0c9-a9dbd60ad309" width="700">

## A first level

1. Draw a floor and walls with the Brush tool in the 2D view ([Brushes](https://github.com/ViciousSquid/Fio/wiki/Basic-concept:--Brushes)).
2. Right-click in the 2D view and add a **Player Start** and a **Light** ([Entities](https://github.com/ViciousSquid/Fio/wiki/Entities-(Things))).
3. Press **F5** (Play). Without a Player Start, Play is refused with a message.
4. **Esc** opens the pause menu (Save, Load, Options, Exit to Editor).

## Shortcuts

Help lists the live set (it is read from the menus, `settings.ini` and `editor/shortcuts.py`); these are the defaults.

| Keys | Action |
| --- | --- |
| Ctrl+Z / Ctrl+Y | Undo / redo |
| Ctrl+C / Ctrl+V | Copy / paste the selection (pasted one grid step away) |
| Shift+Space | Clone the selection |
| Del / Backspace | Delete the selection |
| Esc | Back out: cancel a drag, leave a mode, or deselect |
| H / Shift+H | Hide the selection / show everything hidden |
| Arrow keys | Nudge the selection one grid step |
| Ctrl+K | Drag an arrow from the selected entity to another to link them |
| Ctrl+T / Ctrl+I | Select touching / inside |
| Ctrl+Shift+T / Ctrl+Shift+I | Select partial tall / complete tall |
| Q | Step Select, Brush, Vertex, Edge, Face |
| Shift+V / Shift+E / Shift+F | Vertex / Edge / Face mode |
| Enter / Shift+Enter | Apply the clip / keeping both sides (Clip mode) |
| Page Up / Page Down | Rotate the hovered face's texture 90 degrees |
| G, [ , ] | Show or hide the grid; halve / double its size |
| P | Position window |
| Ctrl+L | Logic Graph |
| Ctrl+Shift+W / Ctrl+Shift+C | Logic wizard / Cutscene wizard |
| Ctrl+Shift+E | Export Game Package |
| ` | Debug console |
| F1, F2, F3 | Connection lines, wireframe, System Monitor (editable in `[Shortcuts]`) |
| F5 | Play / Stop (`key_play_mode`) |

In the Brush tool a plain click starts a new brush, even on an existing one; **Shift+click** selects a brush.

### In Play

| Keys | Action |
| --- | --- |
| W A S D, Space, C, Shift | Move, jump, crouch, sprint |
| E | Use |
| 1 2 3 4 | Select weapon |
| Esc | Pause menu |
| ` | In-game console |
| F6 / F7 | Model collision / monster debug |
| F9 | Split-screen |
| F12 | Full screen (kiosk) on and off |

Console commands can be bound to keys with `bind <key> <command>` ([Console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands)).

## Saving and sharing

File > Save writes the map as JSON ([Map format](https://github.com/ViciousSquid/Fio/wiki/Map-format-(.json))); the editor also autosaves. **Export Game Package** writes a self-contained [`.fiopak`](https://github.com/ViciousSquid/Fio/wiki/.fiopak-archive) that plays in another Fio install and in the [Android player](https://github.com/ViciousSquid/Fio/wiki/Fio-Player-(Android)).
