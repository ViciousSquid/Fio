> Applies to Fio 3.5.0.1111.

<img width="50" height="50" alt="Sprite" title="Default Pickup sprite" src="https://github.com/user-attachments/assets/3f8c7df3-b540-43a5-8a54-8c8bf73ae29b" />

**There is no Pickup entity any more.** Since 3.0.0 a collectable is a [Prop](https://github.com/ViciousSquid/Fio/wiki/Prop) with `collect_enabled` set, and what it gives is described by `collect_type` (and, for items, an [item definition](https://github.com/ViciousSquid/Fio/wiki/Items)).

A map from before 2.5.10 that still contains `pickup` entities loads them as preserved, inert unknown entities (`editor.things.UnresolvedThing`): they are kept in the file but do nothing. Re-author them as collectable Props.

| Old pickup | Prop now |
| --- | --- |
| health | `collect_type: "health"`, `collect_value` |
| key | `collect_type: "key"`, `collect_key_name` (default `blue_key`) |
| `gun1` / `gun2` | `collect_type: "item"`, `collect_item: "gun1"` / `"gun2"` |
| `cig` | `collect_type: "item"`, `collect_item: "custom1"` (`cig` was renamed; `custom2` is new) |

Maps that said `collect_type: "weapon"` with `collect_weapon` are migrated to `"item"` / `collect_item` on load.
