> Applies to Fio 3.5.0.1111.

<img width="45" height="45" alt="Camera" title="Camera" src="https://github.com/user-attachments/assets/c1462757-f244-4df0-a6ab-fe2e1abf2b9b" />

A **LogicCamera** (`type: "logic_camera"`, class `editor.things.LogicCamera`) takes the view away from the player for a cinematic and gives it back when it stops. It either plays a [cutscene](https://github.com/ViciousSquid/Fio/wiki/Cutscenes) file or flies along a [PathNode](https://github.com/ViciousSquid/Fio/wiki/PathNodes) chain. Runtime: `engine/cutscene_runtime.py`.

## Properties

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `cutscene_file` | string | `""` | A cutscene to play, as a project-relative path inside `cutscenes/` (`cutscenes/showcase.json`). In a played package it is read from the package. |
| `path_target` | string | `""` | PathNode to start at when no cutscene file is given. |
| `speed` | float | `200.0` | Travel speed along the path. |
| `fov_override` | float | `0.0` | Field of view; `0` keeps the default. |
| `look_ahead` | bool | `true` | Face the next node while travelling. |
| `lookat_return_time` | float (s) | `5.0` | Time to return after a `LookAt`. |

## Inputs and outputs

| Input | Effect |
| --- | --- |
| `Start` | Begin the sequence. |
| `Stop` | Abort and return the view to the player. |
| `Pause` / `Resume` | Freeze / continue. |
| `SetSpeed` (float) | Change travel speed. |
| `LookAt` (string) | Look at the named entity. |

Outputs: `OnStart`, `OnReachNode`, `OnFinished`.

## The player's own camera

The play camera has a first-person and an overhead (top-down) mode (`engine/logic_camera.py`); the console command `cam [overhead|fp] [seconds]` tweens between them.
