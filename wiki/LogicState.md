> Applies to Fio 3.5.0.1111.

# LogicState

<img width="45" height="45" alt="LogicState" title="LogicState" src="https://github.com/user-attachments/assets/fec15fb9-afa8-4b7b-8bd2-b3bf3e7e723f" />

`LogicState` is Fio's state primitive: a named store of typed key/value pairs that is read, written, compared and modified only through [I/O](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) inputs, and that reports every write and every test as an output. It is never evaluated per frame. Stores are identified by `store_name`; all `LogicState` entities with the same store name, in any level, are views of one store held in a process-wide registry.

| | |
| :--- | :--- |
| Entity class | `editor/things.py: LogicState` (`map_type = 'logicstate'`, `properties['type'] = 'logic_state'`) |
| Value type system | `editor/state_values.py` (no Qt, editor or engine imports) |
| I/O declarations | `editor/io_system.py: register_default_io` (`logic_state`) |
| I/O handlers | `editor/io_handlers.py` (`_STATE_INPUTS`), logic thread |
| Editor panel | `editor/property_editor.py: PropertyEditor._build_keyvalue_group` |
| Place it | 2D view right-click → Logic Entities → **State Store** |

## Properties

| Key | Type | Default | Meaning |
| :--- | :--- | :--- | :--- |
| `store_name` | string | the entity's name when it was created | Which store this entity is a view of. Not updated when the entity is renamed |
| `initial_data` | object | `{}` | Designer defaults: `{key: value}` with JSON-native values. Used only when no store of this name exists yet (see *Persistence*) |
| `capacity` | int | absent → `LogicState.MAX_PAIRS` = 25 | Maximum number of keys; never below 1. The editor's spin box allows 1–4096 |
| `name`, `id`, `io_enabled` | | | Common to all Things |

The Properties tab shows a **Store** field, a **Capacity** spin box and a table with columns *Key*, *Type* (inferred, read-only), *Value* and *State*. Editing the table writes `initial_data` only; live values are displayed, not edited. *State* is `default` (a designer default nothing has written), `set` (written, equal to the default), `changed` (written, different from the default) or `runtime` (created at run time, no default). **Refresh Live Values** redraws the table, **Add Key** appends `new_key` (`new_key_2`, …), **Remove Selected** deletes a row, and a **Preset key** row appears when a plugin suggests keys for the store.

## Value types

Six types (`state_values.STATE_TYPES`):

| Type | Stored as | Written in a parameter as |
| :--- | :--- | :--- |
| `int` | Python `int` | an integer literal: `5`, `-12`, `+3` |
| `float` | Python `float` | a float literal: `2.5`, `.5`, `1e3` |
| `bool` | Python `bool` | `true` / `false`, `yes` / `no`, `on` / `off` (case-insensitive) |
| `null` | `None` | `null` or `none` (case-insensitive) |
| `uuid` | the canonical string | `8-4-4-4-12` hex digits; a string that matches this pattern *is* a `uuid` |
| `string` | the text, unchanged | anything else |

I/O parameters are text, so `state_values.parse` infers the type in this order: bool words, null words, integer literal, float literal, otherwise string. Matching ignores surrounding whitespace, but a value that matches nothing is stored exactly as given. To prevent inference, name the type in the key: `door_code:string=007`. An explicit type that the text cannot be converted to (`hp:int=abc`, `flag:bool=maybe`, `ref:uuid=xyz`) refuses the write.

On the wire (`state_values.format_value`): booleans are `true` / `false`, null is `null`, floats use Python `repr` (`2.0`, `3.5`), integers and strings are themselves.

Values loaded from a file are never re-typed. A legacy store holding the string `"10"` keeps the string; comparison and arithmetic understand numeric strings.

## Inputs

Parameters are split on the **first** `=` or `,`. An empty parameter makes every input except `ClearAll`, `GetValue`, `GetObjectValue` and `ClearObjectState` do nothing.

| Input | Parameter | Effect and outputs |
| :--- | :--- | :--- |
| `SetValue` | `key=value`, `key:type=value`, or bare `key` (stores integer `1`) | Writes the value. Outputs: *write outputs* (below) |
| `GetValue` | `key` | `OnValueRead` with the value, or `OnKeyNotFound` (payload: the key; none when the parameter is blank) |
| `ClearKey` | `key` | Removes the key. If it existed: `OnValueCleared`, then `OnKeyCleared` (payload: the key). Otherwise nothing |
| `ClearAll` | — | Removes every key. Fires nothing |
| `CopyFrom` | store name | Copies every pair of another store from the registry, stopping at the first new key that would exceed capacity. Fires `OnValueSet` with no payload if that store exists; nothing otherwise |
| `CopyValue` | `from,to` or `store.from,to` | Copies one value to key `to`, optionally from another store. Source missing: `OnKeyNotFound` (payload: source key). Otherwise *write outputs* for `to`. A `.` in the source is always read as the store separator |
| `Increment` | `key` or `key,amount` (default 1) | Adds. *Write outputs* |
| `Decrement` | `key` or `key,amount` (default 1) | Subtracts. *Write outputs* |
| `Add` | `key,amount` (default 1) | Adds. *Write outputs* |
| `Subtract` | `key,amount` (default 1) | Subtracts. *Write outputs* |
| `Multiply` | `key,factor` (default 1) | Multiplies. *Write outputs* |
| `Divide` | `key,divisor` (default 1) | Divides; the result is always a float. Division by zero leaves the value unchanged and fires `OnStoreFull` |
| `Min` | `key,value` (default 0) | Stores the smaller of the current value and the operand. *Write outputs* |
| `Max` | `key,value` (default 0) | Stores the larger. *Write outputs* |
| `Clamp` | `key,low,high` | Confines the value to the range (reversed bounds are swapped). Fewer than three fields: ignored. *Write outputs* |
| `Toggle` | `key` | Inverts the value as a boolean and stores a `bool`. *Write outputs* |
| `Compare` | `key<op>value`, op one of `>=` `<=` `==` `!=` `>` `<` | Tests the key. Missing key: `OnKeyNotFound` (payload: key). Otherwise the *branch outputs* with the **stored** value as payload. No operator in the parameter: an error is logged and nothing fires |
| `TestValue` | as `Compare` | Legacy name for `Compare` (same handler) |
| `Exists` | `key` | *Branch outputs*: true if the key is present (payload: the key) |
| `Missing` | `key` | *Branch outputs*: true if the key is absent (payload: the key) |
| `SetObjectValue` | `key=value` (same forms as `SetValue`) | Writes to the object-local key of the firing entity. *Write outputs*, whose payload names the full object key. No firing entity: an error is logged |
| `GetObjectValue` | `key` | `OnValueRead`, or `OnKeyNotFound` (payload: the full object key; none when the parameter or the firing entity is missing) |
| `ClearObjectState` | — | Removes every object-local key of the firing entity. If any existed: `OnValueCleared`, then `OnKeyCleared` (payload: the entity's UUID) |

**Write outputs.** A refused write fires `OnStoreFull` (payload: the key). An accepted write fires `OnValueSet` (payload `key=value`), then, only if the stored value actually changed, `OnValueChanged` (same payload). A write is refused when the key is blank, when it would add a new key to a store already at capacity, when an explicit type does not fit, or when the arithmetic is undefined (division by zero). A full store still accepts writes to keys it already holds.

**Branch outputs.** True fires `OnTrue` then `OnCompareTrue`; false fires `OnFalse` then `OnCompareFalse`.

Arithmetic rules (`state_values.arithmetic`, `LogicState.arithmetic`):

* A missing key, or a current value that is not numeric, counts as `0`; so `Increment` on a fresh key stores `1`. A non-numeric operand counts as `0` (as `1` for `Decrement`).
* Numeric strings are read as numbers, so a legacy `"4"` incremented becomes the integer `5`. Booleans count as `0` / `1`.
* Integer operands give integer results for everything except `Divide`.
* `Toggle`: a missing key or `null` becomes `true`; a bool is inverted; a number becomes `false` if non-zero, `true` if zero; the words `true/yes/on` become `false` and `false/no/off` become `true`; any other non-empty string becomes `false`.

## Outputs

| Output | Payload | Fired when |
| :--- | :--- | :--- |
| `OnValueSet` | `key=value` (none for `CopyFrom`) | Every accepted write |
| `OnValueChanged` | `key=value` | An accepted write changed the stored value or its type |
| `OnValueRead` | the value | `GetValue` / `GetObjectValue` found the key |
| `OnValueCleared` | key (or entity UUID) | A key was removed by `ClearKey` / `ClearObjectState` |
| `OnKeyCleared` | same | Legacy name; always fired immediately after `OnValueCleared` |
| `OnStoreFull` | key | A write was refused (see *Write outputs*) |
| `OnKeyNotFound` | key, when known | A read, comparison or copy targeted a missing key |
| `OnTrue` | stored value (`Compare`) or key (`Exists`, `Missing`) | A test passed |
| `OnFalse` | same | A test failed |
| `OnCompareTrue` / `OnCompareFalse` | same | Legacy names; fired immediately after `OnTrue` / `OnFalse` |

Payloads reach a connection only when its own parameter is blank (parameter pass-through). `OnValueRead → LogicCommand.RunCommand` with a blank parameter, for example, runs the stored string as a console command.

Change detection (`things._same_value`) compares type as well as value: writing integer `1` over boolean `true` is a change, and so is `5` over `5.0`.

## Comparison semantics

`state_values.compare(actual, expected, operator)`; the expected side is typed with the same `parse` rules as a stored value.

1. If either side is `null`: `==` is true only when both are null, `!=` is its negation, and every ordered operator is false.
2. If **both** sides are numeric — a number, a bool, or a string that is an integer or float literal — the comparison is numeric (`"10" >= 9` is true; `true == 1` is true).
3. Otherwise both sides are compared as their wire strings: case-sensitive equality, and lexicographic ordering for `>`, `<`, `>=`, `<=`.

The parameter is scanned for operators in the order `>=`, `<=`, `==`, `!=`, `>`, `<`, splitting at the first one found; a single `=` is not an operator.

## Object-local state

`SetObjectValue`, `GetObjectValue` and `ClearObjectState` address keys namespaced by an entity's UUID: `@<uuid>/<key>` (`LogicState.object_key`, prefix `OBJECT_KEY_PREFIX = '@'`). The UUID is the chain's **activator** (`IOManager.current_activator_id()`), falling back to the immediate source — so a monster's `OnDeath` routed through a LogicRelay still records against the monster. These are ordinary keys in the same store: they count towards capacity, appear in the editor table, and are read by `GetValue` with the full key.

## Persistence

* **The registry.** `LogicState._persistent_registry` is a class-level `dict` mapping `store_name` to its values. It lives for the life of the process and is not cleared when Play starts or stops or when a level is loaded. That is how state crosses a [LevelChanger](https://github.com/ViciousSquid/Fio/wiki/LevelChanger): the next level's store of the same name is constructed, finds the registry entry and adopts it.
* **Construction** (`_sync_from_persistent`): if the registry has `store_name`, its values are loaded; otherwise `initial_data` is copied. A registry entry therefore always wins over the designer defaults — including an entry that `ClearAll` left empty.
* **Writes** update the entity's `_runtime_data` and, when the value changed, the registry (`_sync_to_persistent`). Entities with an empty `store_name` never touch the registry.
* **Serialisation.** `LogicState.to_dict()` pushes the live values to the registry and writes them to the record as `runtime_data` (keys sorted, values JSON-native via `state_values.save_store`). `LogicState.from_dict()` restores `runtime_data` into the entity and the registry.
* **What loaders do.** The level loader and the save-game restore both construct Things through `Thing.from_dict`, which instantiates the class directly and does not read `runtime_data`; `engine/savegame.py` overlays only `pos` and `properties`. Consequently a level opened in a fresh process starts from `initial_data`, and loading a save game does not roll a store's values back: within one process the store keeps whatever the registry holds. The `runtime_data` block is written to maps and saves but is only consumed by a direct `LogicState.from_dict` call.
* **Plugins** share the same registry through `api.global_store` (values are returned to plugins as strings).

```json
{
  "type": "logic_state",
  "pos": [0, 0, 0],
  "properties": {
    "name": "World", "id": "…", "type": "logic_state", "io_enabled": true,
    "store_name": "World",
    "initial_data": {"killed": 0}
  },
  "io_connections": [],
  "runtime_data": {"door": true, "killed": 0}
}
```

## Python API

On a `LogicState` instance (logic thread):

| Method | Returns |
| :--- | :--- |
| `get_value(key, default="<missing>")` | The typed value, or `default` (`LogicState.MISSING`) |
| `has_key(key)`, `value_type(key)`, `get_all_pairs()`, `get_pair_count()` | Presence (a key holding `null` exists), type name, copy of the pairs, count |
| `apply_value(key, value, value_type=None)` | `(ok, changed, stored)` |
| `set_value(key, value, value_type=None)` | `ok` |
| `arithmetic(key, operation, operand)` | `(ok, changed, stored)`; operation is one of `add`, `subtract`, `multiply`, `divide`, `min`, `max` |
| `clamp(key, low, high)`, `toggle(key)` | `(ok, changed, stored)` |
| `increment(key, amount=1)`, `decrement(key, amount=1)` | the new value |
| `compare(key, operator, expected)` | `(found, result)` |
| `clear_key(key)`, `clear_all()`, `copy_from(store)`, `copy_value(src, dst, source_store=None)` | as named |
| `set_object_value(entity_id, key, value, value_type=None)`, `get_object_value(entity_id, key, default)`, `clear_object_state(entity_id)` | object-local variants |

Outside handlers, `IOManager.query_keyvalue(store_name, key, default="<missing>")` reads a value (from the entity **named** `store_name` if one is in the scene, else from the registry) and `IOManager.set_keyvalue(store_name, key, value)` writes one (entity lookup by name only).

## Example

```text
Monster.OnDeath      → World.Increment   "killed"
World.OnValueChanged → World.Compare     "killed>=5"
World.OnTrue         → BossDoor.Open                 (fire once)
```

`OnValueChanged` carries `killed=<n>`, but the `Compare` connection has its own parameter, which takes precedence. `OnTrue` fires on the fifth kill and on every kill after it, so the door connection is marked fire-once.

## Tests

`tests/logic/test_logic_state.py` (every input and output, capacity, object-local state, the registry), `tests/logic/test_state_values.py` (parse, coerce, compare, arithmetic), `tests/persistence/test_logic_round_trip.py`.

## See also

[Logic Entities](https://github.com/ViciousSquid/Fio/wiki/Logic-Entities) · [Triggers & The I/O Logic System](https://github.com/ViciousSquid/Fio/wiki/Triggers-&-The-I-O-Logic-System) · [Save Games](https://github.com/ViciousSquid/Fio/wiki/Save-Games) · [Plugin API](https://github.com/ViciousSquid/Fio/wiki/Plugin-API)
