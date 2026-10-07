
#### World portal for non-Euclidean environmental design -  _Inspired by Prey (2006)_


Portals act as windows which the player can step through. Use this to make endless corridors or similar confusing designs.

> Renderer intensive, use sparingly

> In the editor a portal never displays a sprite: its aperture wireframe shows where it is and how big it is.

> ### Demonstrated in `maps/Portal_Test.json`


Also see: [portal-specific console commands](https://github.com/ViciousSquid/Fio/wiki/Console-commands#portals-1260)

---------------------

* `Attach to mover`
* `Portal Target` - Name of partner Portal
* `Active`
* `Angle`
* `Color`
* `Height`
* `Visible`
* `Width`


----------------
### Inputs

| Input | Description |
| :--- | :--- |
| `Enable` | Sets `active` to `True` and triggers the `OnActivate` event |
| `Disable` | Sets `active` to `False` and triggers the `OnDeactivate` event |
| `Toggle` | Flips the current `active` state |

### Outputs

| Output | Description |
| :--- | :--- |
| `OnPlayerEnter` | Fired once per transit when the player passes through |
| `OnActivate` | Fired when the portal is enabled |
| `OnDeactivate` | Fired when the portal is disabled |


<img src="https://github.com/ViciousSquid/Fio/blob/2.2.0.2408/assets/__portal.gif" width="300">

see also:  [Portal Rendering ‐ Technical Overview](https://github.com/ViciousSquid/Fio/wiki/Portal-Rendering-%E2%80%90-Technical-Overview)



