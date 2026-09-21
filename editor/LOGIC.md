# Fio's event-driven world model

Fio has no scripting language, no VM and no gameplay runtime. What it has
instead is a small set of entity primitives and one communication mechanism,
and the "program" a designer writes is the network of connections between them.

This document is the map of that system: what each primitive is responsible
for, what changed in 2.4, and — at the end — the architectural review the 2.4
work was done against, including what was deliberately *not* built.

---

## 1. The model

```
I/O                       communication
LogicState                persistent state
LogicRelay                event routing
LogicGate                 transient boolean logic
LogicTimer                temporal events
LogicSpawner              entity creation
LogicCommand              controlled console bridge
Trigger / TriggerBrush    world event sources
Monster / Door / Pickup   actual world behaviour
```

Every one of those answers exactly one question. Complexity comes from wiring
them together, not from any of them growing:

```
entity event -> I/O -> LogicState -> state-derived event
             -> LogicRelay / LogicGate / LogicTimer -> I/O -> world entity
```

Nothing in that chain needs to understand the rest of it, and the engine never
learns what the chain *means*. The engine provides the primitives, the map
provides the composition, the game provides the meaning.

### What is not here, on purpose

There is no `LogicController`, no quest system, no objective manager, no
world-event manager and no global gameplay tick. There is no second event bus,
no second persistence database and no expression language. `tests/logic/
test_logic_composition.py` asserts their absence, because a missing manager is
not something a behavioural test would ever notice.

---

## 2. I/O

A connection is `output -> (target, input, parameter, delay, fire_once)`.
Targets are resolved by **UUID first, name second**, and resolution happens
when the event fires, not when it was queued.

Three properties of that are load-bearing:

* **A rename never breaks a wire.** Names are a human convenience; identity is
  the UUID.
* **A delayed event survives the world changing under it.** The target it names
  may have been parked by Big World in the meantime — it is the same object
  with the same UUID, and it still receives the input.
* **Parameter pass-through moves values.** A connection whose own parameter is
  blank inherits the value the output fired with. This is Fio's entire value-
  substitution mechanism, and the reason it needs no templating language:

  ```
  Button.OnTrigger -> State.GetValue("speed")
  State.OnValueRead -> Lift.SetSpeed          (blank parameter: inherits 250)
  ```

### Source and activator

A handler can ask the I/O manager who fired it:

* `current_source()` — the entity one hop back.
* `current_activator()` — the entity that *began* the chain.

They differ only once a chain is longer than one hop, which is exactly when it
matters. A monster's death routed through a relay should still record against
the monster; the relay is plumbing. Object-local state uses the activator.

### Validation

`validate_scene_connections(brushes, things)` reports three kinds of problem:

| code | meaning |
| --- | --- |
| `missing_target` | the connection points at an entity that is not in the map |
| `unknown_input` | the target's type does not accept that input |
| `unknown_output` | the source's type does not have that output |

**Tools -> Validate All Connections** runs it over the whole map; the I/O
editor flags the offending row as you author it.

One distinction is deliberate and easy to lose. A type that Fio has *no*
definitions for (a plugin entity that registered none) is not judged at all —
reporting everything on it would train people to ignore the report. A type that
*is* registered and declares no inputs **is** judged: every input on it is
wrong, and saying so is the point. `is_registered_type()` is what separates the
two; the length of the declared list cannot. The inputs every entity accepts
regardless of type (`IOManager.GENERIC_INPUTS`) are accepted too, and a test
asserts that set still matches what the dispatcher actually implements.

### Declaration vs implementation

`IO_REGISTRY` is the **declaration** — what the editor offers a designer.
An `IOManager`'s handler table is the **implementation**. They are separate
structures and have to be: definitions are global and built at import, handlers
are per-session and partly supplied by plugins at runtime.

Two structures can drift, and a drifted declaration is invisible in the worst
way — the editor offers the input, a designer wires it, nothing runs, and no
error appears anywhere. `is_registered_type()` fixes one symptom of the split;
it does not prove the two agree. So the agreement is checked rather than
assumed, from both directions:

* **`audit_io_coverage(io_manager)`** reconciles them, returning declared inputs
  with no implementation, implementations with no declaration, and handlers
  registered against a type the registry has never heard of (a typo in a type
  token, which silently makes a handler unreachable). Plugin authors can call
  it against their own entities.
* **`fire_output` reports an undeclared output** — one the code fires that no
  type declares, so it works for whoever knows the name and exists for nobody
  reading the editor. Checked under the existing debug gate, against a memoised
  set, on a path that was already logging.
* **`tests/io/test_io_contract.py`** asserts zero drift in the whole registry,
  and goes further than the tables: it *invokes* every declared input through
  the real dispatcher on a real instance of its type, and fails if one reaches
  no implementation or raises. A handler that throws is not an implementation —
  `_execute_input` swallows the exception, so the input silently does nothing in
  exactly the way an unimplemented one does.
* Every declared **output** is checked to have an emission site in shipped code,
  following wrappers (the Tidy plugin fires everything through its own `_fire`,
  and an output is no less implemented for going through one).

The one sanctioned exception is **`ABSTRACT_IO`**, a table of declared I/O that
is inert on purpose, each entry carrying its reason. The tests read that table
rather than keeping a list of their own, so an exception is stated once, next to
the declaration. It is currently empty: every declared input runs something and
every declared output is fired.

The probe's stand-in for `LogicThread` is itself checked against the real class,
so it cannot drift into testing a fiction — a stub that grew an attribute the
real host lacks would let every handler reaching for it pass here and fail in
the game.

---

## 3. LogicState

The state entity was renamed `LogicState` in 2.4. 2.5 drops the pre-2.4 alias:
there is one name for the class, and `logicstate` is the token written to map
files.

> `LogicState` is persistent named state that can be read, modified, compared,
> and used to generate I/O events. It is not a controller, a quest manager, an
> event bus or a per-frame evaluator.

### Values are typed

`string` · `int` · `float` · `bool` · `null` · `uuid`

I/O parameters are text, so a value's type is inferred from what is written —
`5` is an integer, `true` a boolean, `null` is null — and can be stated
outright when inference is not wanted:

```
SetValue    door_code:string=007
```

Legacy stores held only strings and are **not** rewritten on load, because
comparison and arithmetic already understand them: `"10" >= 9` is true and
`Increment` on `"4"` gives `5`. A map authored against the old store behaves
the same or better, never worse.

### Operations

Parameterised, so the set stays small and a map can express something the
engine was never told about:

| input | parameter |
| --- | --- |
| `SetValue` | `key=value`, `key:type=value`, or a bare `key` |
| `GetValue` / `ClearKey` / `Toggle` / `Exists` / `Missing` | `key` |
| `Increment` / `Decrement` / `Add` / `Subtract` / `Multiply` / `Divide` / `Min` / `Max` | `key,amount` |
| `Clamp` | `key,low,high` |
| `Compare` | `key>=value` (also `== != > < <=`) |
| `CopyValue` | `from,to` or `store.from,to` |
| `CopyFrom` | another store's name |
| `SetObjectValue` / `GetObjectValue` / `ClearObjectState` | keyed by the chain's activator |

`Increment("killed", 1)` rather than `IncrementKillCounter` — the engine has no
idea what is being counted, and that is the correct amount of knowledge.

### Events

| output | when |
| --- | --- |
| `OnValueSet` | every accepted write |
| `OnValueChanged` | only when the stored value actually moved |
| `OnValueRead` | `GetValue`, carrying the value |
| `OnValueCleared` | a key was removed |
| `OnStoreFull` | a write was refused |
| `OnKeyNotFound` | a read or comparison hit a missing key |
| `OnTrue` / `OnFalse` | a `Compare`, `Exists` or `Missing` result |

`OnCompareTrue` / `OnCompareFalse` / `OnKeyCleared` still fire under their
pre-2.4 names.

The changed/unchanged distinction is what makes watchers unnecessary: a chain
hung off `OnValueChanged` runs on real transitions, and nothing has to notice
them after the fact.

There is deliberately no `OnValueGreaterThanZero` and no
`OnValueIsExactlyFive`. `Compare` plus ordinary I/O covers all of it.

### Object-local state

Not a second database and not a per-entity framework: ordinary keys in the same
store, namespaced by the object's UUID (`@<uuid>/<key>`). So it persists, saves,
transitions levels and appears in the Property Manager exactly like every other
value, and a dormant entity's state is simply data that outlives its dormancy.

### Persistence

Values live in a process-wide registry keyed by `store_name`. Two stores that
share a name in different levels are the same store. Plugins reach the same
registry through `api.global_store` — one registry, no copies. Serialisation is
JSON-native, key-sorted and deterministic; a saved store is readable as exactly
what it is.

---

## 4. The other primitives in 2.4

**LogicRelay.** `fire_once` was a property nothing read, so one-shot relays
fired every time. It latches now, and `Reset` re-arms it. The parameter rides
through, so a relay stays transparent to what it routes.

**LogicGate.** Three fixes. Signal sets are keyed by UUID rather than name, so
two gates sharing a name are two gates. `Trigger` now *asserts* a signal
idempotently — it used to flip it, so a source reporting twice un-asserted
itself and an AND gate could never close (`ToggleInput` keeps the old
behaviour). And the expected-input count now counts *connections* calling
`Trigger`, addressed by UUID as well as by name: the old count matched names
only, so a gate wired the way the editor actually wires things saw zero
expected inputs, fell back to one, and fired on its first signal. It reads the
cached reverse-target index instead of walking the level on every signal.

**LogicTimer.** Countdown state is keyed by UUID, not `id(entity)` — a memory
address cannot be saved and can be reused by an unrelated object. `one_shot`
fires once and stops itself (`OnFinished`). The update walks a precomputed
timer list instead of isinstance-testing every thing in the level each frame.

**Doors and movers.** `Stop` and `Reverse`, and `Open`/`Close` now interrupt
motion instead of ignoring it — a door caught mid-close used to ignore `Open`
until it had finished shutting.

**Monsters.** `Sleep`, `SetHealth`, `Respawn` (+ `OnRespawn`). Nothing here
invents behaviour: each maps onto properties the AI already owns, and spawn
health is recorded during the reset pass that already walks every monster.

---

## 5. Big World and dormancy

A dormant entity incurs no state evaluation, no condition evaluation, no I/O
polling and no watcher processing — because none of those things exist. The
only per-frame path the logic system has is draining the delayed-event queue
and advancing running timers.

One real problem did need fixing. Parking an entity forces `hidden` and
`disabled` on and stashes what the map authored; unparking restores the stash.
An I/O event that wrote those flags straight onto a parked entity was therefore
thrown away at the next unpark — the one way an event-driven world can lose a
change with nothing reporting an error. The generic inputs write through
`engine.spatial.set_authored_flag`, which updates whichever of the two the
object is actually using.

So the smallest coherent behaviour for *event scheduled → target becomes
dormant → event fires* is: it fires, on time, into the entity's persistent
state, and the change survives the entity waking up. No second scheduler, and
no waking an entity to deliver it.

Delayed events and timer countdowns are now saved and restored with the game,
rebased on the live clock and addressed by UUID.

---

## 6. Worked examples

**Kill five monsters.**

```
Monster.OnDeath      -> State.Increment("killed")
State.OnValueChanged -> State.Compare("killed>=5")
State.OnTrue         -> Door.Open
```

**A locked door.**

```
Switch.OnTrigger   -> State.SetValue("door_unlocked=true")

Player.UseDoor     -> State.Compare("door_unlocked==true")
State.OnTrue       -> Door.Open
```

The door does not know what a key is; the store does not know what a door is.

**A two-switch puzzle.** Each switch writes its flag and feeds the gate; the
puzzle is in the wiring, not in the store.

```
SwitchA.OnTrigger -> State.SetValue("switch_a=true")
SwitchA.OnTrigger -> Gate.Trigger("a")
SwitchB.OnTrigger -> State.SetValue("switch_b=true")
SwitchB.OnTrigger -> Gate.Trigger("b")
Gate.OnTrigger    -> Door.Open          (AND)
```

**A timed encounter.**

```
Trigger.OnTrigger -> Relay.Trigger -> Timer.Enable -> Spawner.Spawn
```

**A persistent world change.** Set it once; react to it on every later load.

```
Trigger.OnTrigger -> State.SetValue("bridge_destroyed=true")

LevelStart        -> State.Compare("bridge_destroyed==true")
State.OnTrue      -> Bridge.Disable
```

---

## 7. Architectural review

The questions the 2.4 work was required to answer before any code was written.

**What could the existing I/O system already do?** Nearly everything: delivery,
delay, `fire_once`, UUID-first target resolution with a name fallback, and
parameter pass-through. It needed no new abstraction — only an activator
alongside the source, dormancy-safe generic writes, queue persistence, and a
way to report a broken wire at edit time instead of at run time.

**LogicRelay?** Routing, enable/disable/toggle, cancel-pending. Missing:
honouring its own `fire_once`, and `Reset`.

**LogicGate?** AND/OR/XOR/NAND/NOR, all correct in principle. Its input
*counting* was broken for UUID-addressed connections and scanned the whole
level per signal, and `Trigger`'s flip semantics made AND gates unusable.

**LogicTimer?** Intervals and enable/disable. Missing: one-shot, stable keying,
and not scanning the level each frame.

**What was genuinely missing from the pre-2.4 store?** Typed values; change
detection; `Exists`/`Missing`; arithmetic beyond ±1; `Compare` producing plain
`OnTrue`/`OnFalse`; a capacity that was not a hard 25; and — the largest defect
— it could not be loaded from a map file at all. Its serialised type token
matched no class, so every store placed in a map was silently dropped on load.
Declaring `map_type` on the class is what fixes that, for it and for any future
rename: the token a class writes is stated rather than inferred from its name.

**What belongs in `LogicState`?** Storage, typing, arithmetic, comparison,
change events, persistence. Nothing else.

**What belongs elsewhere?** Door `Stop`/`Reverse`, mover `Reverse`, monster
`Sleep`/`SetHealth`/`Respawn`, relay `Reset`, timer one-shot. Each in the
entity that owns the behaviour.

**What would have duplicated an existing primitive?**

* *Conditional I/O connections* (§12 of the brief). Rejected. `Compare ->
  OnTrue -> Gate` already composes, and an embedded condition on every
  `OutputConnection` would be an expression language reached by a different
  route.
* *Persistent watchers indexed by store and key* (§11). Rejected. Explicit
  `Compare` is sufficient, and `OnValueChanged` already gives edge-triggered
  behaviour without anything standing by to notice it.
* *A state-value templating syntax in parameters* (§13). Rejected.
  `GetValue -> OnValueRead -> blank parameter` is the mechanism, and it already
  existed.
* *Separate counter / branch / global entities.* Rejected. `LogicState` covers
  counters, branches and globals in one primitive; adding three more would be
  three entities where Fio has one.

---

## 8. Comparative notes: TrenchBroom and Source

Both were read as **references, not templates**. Fio's I/O, UUID identity,
`LogicThread`, persistence and entity model are authoritative; nothing below
replaced a Fio mechanism with a foreign one.

### Concepts adopted, expressed in Fio's own structures

| From | Concept | How it lands in Fio |
| --- | --- | --- |
| Source | `FireOutput(value, pActivator, pCaller, delay)` distinguishes the entity that *began* a chain from the one firing this hop | Fio had only the caller. The activator is two fields on the existing `IOManager` and one on the existing `PendingEvent` — no new structure — and it is what makes object-local state record against the monster rather than the relay. |
| Source | `CEventQueue::Save/Restore` persists in-flight events | Fio's queue lived only in memory, so a save taken mid-delay lost the event. Now captured in `engine/savegame.py` with the time *remaining* and Fio's UUIDs — which beat Source's `EHANDLE`s, since a UUID survives a map change. |
| TrenchBroom | Dangling links are a reported **issue** with a quick fix, not a silent failure | Fio already flagged an unresolvable target in the I/O editor. What it never checked was the *input name*, so a typo resolved its target and then did nothing. Now reported, from Fio's own `IO_REGISTRY` and cached reverse index. |
| TrenchBroom | `ValueState` (unset / set / differing) on a property row, and `Unknown` for properties the definition does not declare | The `LogicState` panel is one table with a state column, and a key created at run time gets a row instead of being invisible. |
| TrenchBroom | `newPropertyKeyForEntityNodes` suggests an unused key | Adding a row twice no longer silently collides on `new_key`. |

Two further Source ideas were noted and **not** taken: `math_counter`'s
`OnHitMin`/`OnHitMax` edge semantics are already covered generically by
`OnValueChanged`, and `logic_branch`'s `SetValue`/`SetValueTest` pair is already
covered by having `Compare` as a separate input.

### Concepts rejected because Fio has an equivalent

| From | Their mechanism | Fio's equivalent |
| --- | --- | --- |
| TrenchBroom | `EntityLinkManager`, a bidirectional index of link ends keyed by property key | `target_index` / `find_targeting_sources`, cached against a revision counter. A second index would be a second thing to invalidate. |
| TrenchBroom | `PropertyValueTypes::LinkSource` / `LinkTarget` — links modelled *as properties* | `OutputConnection.target_id`. Links are connections, not properties; adopting this would mean re-modelling I/O as properties, which is a parallel implementation of I/O. |
| TrenchBroom | `SmartPropertyEditorManager`, a per-type editor plug-in framework | Fio's property-editor builder, which the brief requires preserving. |
| TrenchBroom | Numbered properties (`target`, `target2`, …) | A connection *list*, which already holds many connections per output and encodes the same thing better. |
| TrenchBroom | `el::Expression`, a real expression language | Excluded by design. `Compare` + `LogicGate` is the composition story. |
| Source | `EHANDLE` entity references | UUIDs, which persist across save/load and map transitions. |
| Source | `variant_t` typed output values | A string wire plus `editor.state_values` typing. A second value model would duplicate it. |
| Source | `g_EventQueue`, a global queue | `IOManager.pending_events`, owned by the manager rather than by the process. |
| Source | `logic_auto`, `math_counter`, `logic_branch`, `env_global` as four entities | `LogicState` covers all four. |
| Source | `logic_branch_listener`, which watches branches and fires when the combination changes | Exactly the watcher the brief warns against. `LogicGate` plus explicit `Compare` covers it with no standing evaluation. |
| Source | `CEventAction::m_nTimesToFire` (an int) vs Fio's `fire_once` (a bool) | Considered; not adopted. Nothing in the 2.4 scope needs a count, and widening the serialised connection format for it would be change without a reason. |

---

## 9. Performance

The logic system's entire per-frame cost is: drain the delayed-event queue
(usually empty), and decrement the countdown of each *enabled* timer. There is
no state scan, no condition scan, no watcher scan, no background thread and no
VM. A level full of switched-off timers costs one flag read each; a level with
none costs nothing.

`tests/logic/test_logic_composition.py` asserts that the play-mode tick does
not so much as mention the state store, which is the guard that keeps it that
way.
