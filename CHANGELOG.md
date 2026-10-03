# Changelog

## 2.5.11

### Behaviour changes

- **Prop collection default:** the default `collect_type` for a Prop is now
  `weapon` rather than `health`. Maps that omit an explicit
  `collect_type` therefore collect as a weapon after upgrading to 2.5.11.
  Explicitly authored collection types are unchanged.

### Engine fixes

- Preserve Effect runtime state when authored Effect rows are rebuilt after
  insertions or reordering.
- Share capacity growth between dense Effect and Projectile stores without
  repeating old data into newly allocated rows.
- Use a bounded nearest-enemy kernel crossover for small populations and
  many tiny teams.
- Vectorise moved Effect position synchronisation into the EffectStore.
