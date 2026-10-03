<img width="45" height="45" alt="Camera" title="Camera" src="https://github.com/user-attachments/assets/c1462757-f244-4df0-a6ab-fe2e1abf2b9b" />
LogicCamera

#### First-person camera that moves along a path (for cutscenes)
Takes control away from the player, returns it when stopped

> ### Demonstrated in example map: `maps/Maze.json`

---------------------

`Path Target:` [PathNode](https://github.com/ViciousSquid/Fio/wiki/PathNodes) to start at

`Speed:` camera movement speed

`FOV Override:` camera FOV (default 90)

`Look at next node` - True/False - as the camera moves, should it focus on the next PathNode?

----------------
### Inputs

| Input | Description |
| :--- | :--- |
| `Start` | Begin the cinematic camera sequence |
| `Stop` | Abort and return the camera to the player |
| `Pause` | Freeze the camera at the current chain position |
| `Resume` | Continue a paused sequence |
| `SetSpeed` | Override travel speed [float] |

### Outputs

| Output | Description |
| :--- | :--- |
| `OnStart` | Fired when the sequence begins |
| `OnReachNode` | Fired each time the camera arrives at a PathNode |
| `OnFinished` | Fired when the camera reaches the last node |
