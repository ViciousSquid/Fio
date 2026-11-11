> Applies to Fio 3.1.0.0611 (CANARY).

<img width="50" height="50" alt="image" src="https://github.com/user-attachments/assets/5bee9c20-a701-4d9a-b860-a3a6381a4e79" />

# Speaker

A `Speaker` plays one audio file from `assets/sounds` or `assets/music`, either positioned in the world (volume falls off linearly to zero at `radius`, with stereo panning) or globally (same volume everywhere). Playback is started and stopped through the [I/O system](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) or automatically when Play starts.

| | |
| :--- | :--- |
| Class | `editor/things.py: Speaker` |
| `type` in the map | `speaker` |
| Input handlers | `editor/io_handlers.py` (`speaker_play`, `speaker_stop`, `speaker_toggle`, `speaker_set_volume`) — logic thread |
| Path rules | `engine/speaker_audio.py` (`speaker_sound_path`, `resolve_speaker_sound`, `is_music`, `list_speaker_audio`) |
| Mixer | `engine/view_audio.py: ViewAudio` (`_process_sound_queue`, `_get_sound_instance`, `_spatial_sound_mix`) — UI/render thread, `pygame.mixer` at 44.1 kHz stereo |
| Archive reading | `engine/audio_archives.py` (`AudioArchiveReader`, `read_archive_audio`, `list_archive_audio`) |
| Cross-thread queue | `game_state.queue_sound(dict)` → `game_state.consume_sounds()` once per rendered frame |
| Editor icon | `assets/sprites/speaker.png` |
| Place it | 2D view right-click → **Speaker** |
| Worked example | `maps/Speaker_Test.json` |

## Properties

| Key | Type | Default | Editor | Meaning |
| :--- | :--- | :--- | :--- | :--- |
| `sound_file` | string | `""` | read-only field + **Browse…** (or double-click a file in the Asset Browser's Audio tab) | Project-relative path of the file to play; see *Sound files* |
| `radius` | float, world units | `512.0` | *Radius* spin box | Distance at which a positioned speaker becomes inaudible. Negative values are read as 0, which is silent |
| `global` | bool | `false` | *Global* checkbox | Play without position: no falloff, no panning. `radius` is ignored |
| `show_radius` | bool | `false` | *Show Radius* checkbox | Draw the radius circle in the 2D views (editor only) |
| `volume` | float 0–1 | `1.0` | *Volume* spin box | Volume of this speaker, clamped to 0–1 when mixed |
| `looping` | bool | `false` | *Looping* checkbox | Repeat until `StopSound` |
| `play_on_start` | bool | `false` | *Play On Start* checkbox | Receive `PlaySound` when Play starts |
| `state` | string | `"off"` | *State* text field | `"on"` / `"off"`. Written by `PlaySound` / `StopSound`; read only by `Toggle`. Authoring `"on"` does **not** start playback — use `play_on_start` |
| `name`, `id`, `io_enabled` | | | | Common to all Things |

`global` and `looping` are independent: a positioned sound can loop and a global one can play once.

## Sound files

`speaker_sound_path(value)` normalises `sound_file` and refuses anything outside the two audio folders:

| Authored value | Resolves to |
| :--- | :--- |
| `assets/sounds/door.wav`, `assets/music/theme.ogg` | itself |
| `sounds/engine-room.wav`, `music/theme.ogg` | `assets/sounds/engine-room.wav`, `assets/music/theme.ogg` |
| `door.wav` (bare file name, as older maps hold) | `assets/sounds/door.wav` |
| an absolute path, a drive letter, any `..` component, another folder, an extension other than `.wav` `.ogg` `.mp3` | refused: `PlaySound` logs an error and plays nothing |

Back-slashes are accepted. Sub-folders are allowed (`assets/sounds/ambience/wind.ogg`).

Where the bytes come from (`ViewAudio._get_sound_instance`): if `assets/sounds.fiopak` or `assets/music.fiopak` exists it is **authoritative** for its folder — the file is decoded from the archive in memory, nothing is extracted, and a loose file of the same name is not consulted. Loose files under `assets/sounds/` or `assets/music/` are used only when that folder's archive does not exist. Decoded sounds are cached for the life of the view (`sound_pool`). The contents of `assets/sounds.fiopak` (or the top level of `assets/sounds/`) are preloaded at start-up; music is loaded on first use.

A file under `assets/music/` is *music* and is scaled by the Music volume setting; everything else by the Sound volume setting (`settings.ini` `[Audio] music_volume` / `sound_volume`, 0–100).

## Inputs

| Input | Parameter | Effect |
| :--- | :--- | :--- |
| `PlaySound` | — | Validates `sound_file`, sets `state = "on"`, queues a play request and fires `OnSoundStarted`. With an empty `sound_file` it sets `state = "on"`, logs an error and fires nothing; with a refused path it logs an error and changes nothing |
| `StopSound` | — | `state = "off"`, queues a stop request for this speaker, fires `OnSoundFinished` |
| `Toggle` | — | `StopSound` if `state == "on"`, otherwise `PlaySound` |
| `SetVolume` | float | `volume`, clamped to 0–1. `nan`, `inf` and non-numbers are ignored. Takes effect at the next `PlaySound` |
| `Hide` / `Show` / `ToggleVisibility` | — | Set / clear / flip `hidden`. Affects only the editor sprite; a hidden speaker still plays |
| `Kill`, `Enable`, `Disable`, `SetTint`, `ClearTint` | | Generic inputs; they set flags that do not affect audio |

## Outputs

| Output | Fired when |
| :--- | :--- |
| `OnSoundStarted` | `PlaySound` queued a sound |
| `OnSoundFinished` | `StopSound` ran. It is **not** fired when a non-looping sound reaches its end |

## Runtime behaviour

1. **Play start.** `LogicSession.start_speakers_on_spawn` delivers `PlaySound` (source name `PlayerSpawn`) to every Speaker with `play_on_start`, before the spawn PlayerStart fires `OnPlayerSpawn`, so an `OnPlayerSpawn` connection can override it.
2. **Request.** The handler queues `{action: "play", file, volume, looping, entity_id, position, radius, global}`. `position` is the speaker's position at that moment (`None` for a global speaker); `entity_id` is the Python object identity of the entity.
3. **Mix.** The UI thread drains the queue once per frame. For a positioned sound: `gain = 1 - distance / radius` from the camera (0 at and beyond the radius, 0 when `radius` is 0), and constant-power panning from the horizontal direction to the source relative to the camera's right vector. Final channel volume is `volume × (music or sound volume) × gain` per ear.
4. **Looping sounds** are tracked per speaker: they are re-mixed every frame as the listener moves, a second `PlaySound` restarts the loop instead of stacking a copy, and `StopSound` stops the channel.
5. **One-shot sounds** get their mix once, at the moment they start, and are not tracked: moving afterwards does not change their volume or pan, a second `PlaySound` overlaps the first, and `StopSound` cannot cut one short (it still sets `state` and fires `OnSoundFinished`).
6. **Leaving Play** stops every mixer channel and discards queued requests (`ViewAudio.stop_all_sounds`).

Because the position is captured when the sound starts, a speaker that is moved while its loop plays keeps sounding from where it started until it is re-triggered.

## In the map file

From `maps/Speaker_Test.json` (a global, looping ambience started with the level):

```json
{
  "type": "speaker",
  "pos": [0.0, -192.0, 64.0],
  "properties": {
    "type": "speaker", "name": "Speaker_1",
    "id": "cbc513bd-7671-4904-812e-920970299521",
    "sound_file": "sounds/engine-room.wav",
    "radius": 512.0, "global": true, "show_radius": false,
    "volume": 0.75, "looping": true, "play_on_start": true, "state": "on"
  },
  "io_connections": []
}
```

## Tests

`tests/engine/test_speaker_audio.py` (allowed and refused paths, symlinks, archive listing, music detection), `tests/logic/test_movers_and_speakers.py` (looping, stop, re-trigger, radius gain, `play_on_start`), `tests/io/test_io_contract.py`.

## See also

[Entities (Things)](https://github.com/ViciousSquid/Fio/wiki/Entities-(Things)) · [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) · [Asset Browser](https://github.com/ViciousSquid/Fio/wiki/Asset-Browser)
