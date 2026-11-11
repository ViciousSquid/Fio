> Applies to Fio 3.1.0.0611 (CANARY).

The **Property editor** (side panel; `editor/property_editor.py`) shows and edits whatever is selected: a brush's flags and behaviour, or an entity's properties. Every edit is one undo step.

<img src="https://github.com/user-attachments/assets/e42d5cb6-54a7-46e7-a58f-348001df20a3" width="350">

## Brushes

- **Name**, used as an I/O target.
- **Type**: plain, Trigger, Door, Mover, Water or Fog, with the settings of each ([Brushes](https://github.com/ViciousSquid/Fio/wiki/Basic-concept:--Brushes)): a trigger's type and activation; a door's direction, distance, lip, speed and lock; a mover's motion and path.
- **Subtract** (CSG), **hidden**, **locked**, tint.
- **Outputs**: the brush's [I/O connections](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) (output, target, input, parameter, delay, fire once).

## Entities

One panel per type, generated from the entity's properties: see each entity's page from [Entities (Things)](https://github.com/ViciousSquid/Fio/wiki/Entities-(Things)). Some have their own controls -- a Monster's patrol mode (`loop`, `ping_pong`, `once`) and Customise dialog, a model Prop's Presets and Browse, a Light's colour and shadows, a Portal's partner (linked portals share one active state), a LogicState's typed values.

Plugins describe the properties of their own entities through the [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API), and their panels appear here like any other.

## Behaviour worth knowing

- The panel follows the selection; with several objects selected it edits what they share.
- Numeric fields are held to their ranges.
- Editing while Play is running changes the live world.
- Tooltips can be turned off with `[Editor] property_editor_tooltips`.
- The panel is rebuilt only when the selection or its kind changes; `tests/editor/test_property_editor_rebuilds.py` holds that.
