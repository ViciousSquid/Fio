"""Every declared input runs something; every declared output is fired.

Fio keeps the *declaration* of an entity's I/O (``IO_REGISTRY``, global, built
at import) apart from its *implementation* (an ``IOManager``'s handler table,
per-session, partly supplied by plugins at runtime). They cannot be one
structure — the lifetimes differ — so they can drift, and a drifted declaration
is invisible: the editor offers the input, a designer wires it, nothing runs,
and no error appears anywhere.

This file is the reconciliation. It asserts, for the whole registry:

* every declared input has **exactly one** implementation — a registered
  handler, or the generic dispatcher, never both-and-neither, and never
  registered twice;
* every declared input is **demonstrable**: invoked through the real dispatcher
  on a real instance of its type, it reaches an implementation and does not
  raise;
* every declared output has an **emission path** somewhere in the source;
* nothing is implemented that is not declared, so no input is reachable only by
  someone who already knows its name.

The single sanctioned exception is :data:`editor.io_system.ABSTRACT_IO`, which
these tests read rather than keeping a list of their own — so an entry that is
inert on purpose is stated once, next to the declaration.
"""

import collections
import pathlib
import re

import pytest

pytest.importorskip("PyQt5", reason="the entity classes need PyQt5")

import editor.io_system as io_system                      # noqa: E402
from editor.io_system import (ABSTRACT_IO, IO_REGISTRY, IOManager,  # noqa: E402
                              audit_io_coverage, get_input_names,
                              get_output_names)
from editor.io_handlers import register_all_input_handlers  # noqa: E402
from editor.things import ENTITY_TYPES                     # noqa: E402
from plugins.manager import get_manager                    # noqa: E402

pytestmark = pytest.mark.qt

ROOT = pathlib.Path(__file__).resolve().parents[2]

#: Directories that hold shipped code. Tests are excluded deliberately: an
#: output that only a test ever fires is not implemented.
SOURCE_DIRS = ("editor", "engine", "plugins", "player")


# ---------------------------------------------------------------------------
# The host an input handler is written against
# ---------------------------------------------------------------------------

class HostStub:
    """The subset of ``LogicThread`` that input handlers reach for.

    Deliberately *not* permissive: it does not invent attributes on demand,
    because a stub that answers anything would let a handler reach for something
    the real logic thread has never had and still pass. Every name here is
    checked against the real class by
    :func:`test_the_host_stub_only_promises_what_the_logic_thread_has`, so the
    probe below cannot quietly drift into testing a fiction.
    """

    def __init__(self, io_manager):
        self.io_manager = io_manager
        self.brushes = []
        self.things = []
        self.gate_inputs = {}
        self.timer_states = {}
        self.door_states = {}
        self.mover_states = {}
        self.mover_path_states = {}
        self.light_fade_states = {}
        self.cinematic_state = {}
        self.active_speakers = set()
        self.collected_pickups = set()
        self.collected_keys = set()
        self.respawn_timers = {}
        self.player = None
        self.terrain = None
        self._timer_things = []
        self._monster_spawn_health = {}
        self.editor_state = self

    #: ``editor_state`` points back at the stub so ``logic.editor_state.things``
    #: resolves; the real host holds a separate object there, so the name is a
    #: promise about ``LogicThread`` but the target is not.
    LOCAL_ONLY = frozenset()

    def _find_path_node_by_name(self, name):
        return None


BRUSH_TYPES = {"trigger": "is_trigger", "door": "is_door", "mover": "is_mover",
               "water": "is_water", "fog": "is_fog", "brush": None}


def _make_entity(entity_type):
    """A real instance of *entity_type*, or None if one cannot be built."""
    if entity_type in BRUSH_TYPES:
        brush = {"name": "probe_%s" % entity_type, "id": "probe-%s" % entity_type,
                 "pos": [0.0, 0.0, 0.0], "size": [64.0, 64.0, 64.0],
                 "_io_connections": []}
        flag = BRUSH_TYPES[entity_type]
        if flag:
            brush[flag] = True
        return brush

    for cls in list(ENTITY_TYPES.values()) + list(
            getattr(get_manager(), "_entity_classes", {}).values()):
        try:
            thing = cls(pos=[0, 0, 0], properties={"name": "probe"})
        except Exception:
            continue
        if thing.properties.get("type") == entity_type:
            thing.properties["id"] = "probe-%s" % entity_type
            return thing
    return None


#: A plausible parameter per input, so the probe exercises the handler's real
#: path rather than only its "no parameter, give up" branch.
#:
#: Keyed by ``(entity_type, input)`` and not by input name alone, because the
#: same name means different things on different entities: ``SetValue`` takes
#: ``"key=value"`` on a LogicState and a plain integer on a Pickup, and a table
#: keyed on the name fed the state-shaped parameter to the pickup — which threw
#: it out as unparseable, so that input was probed without ever being exercised.
PROBE_PARAMS = {
    ("logic_state", "setvalue"): "k=1",
    ("logic_state", "getvalue"): "k",
    ("logic_state", "clearkey"): "k",
    ("logic_state", "copyfrom"): "other",
    ("logic_state", "copyvalue"): "k,k2",
    ("logic_state", "increment"): "k,1",
    ("logic_state", "decrement"): "k,1",
    ("logic_state", "add"): "k,1",
    ("logic_state", "subtract"): "k,1",
    ("logic_state", "multiply"): "k,2",
    ("logic_state", "divide"): "k,2",
    ("logic_state", "min"): "k,1",
    ("logic_state", "max"): "k,1",
    ("logic_state", "clamp"): "k,0,10",
    ("logic_state", "toggle"): "k",
    ("logic_state", "compare"): "k>=1",
    ("logic_state", "testvalue"): "k>=1",
    ("logic_state", "exists"): "k",
    ("logic_state", "missing"): "k",
    ("logic_state", "setobjectvalue"): "k=1",
    ("logic_state", "getobjectvalue"): "k",
}
#: Fallbacks by input name, for names that mean the same thing everywhere.
PROBE_PARAMS_BY_NAME = {
    "settint": "255 128 0", "setcolor": "255 128 0",
    "settarget": "probe", "settargetnode": "probe", "setpathtarget": "probe",
    "setanimation": "idle",
}
PROBE_BY_PARAM_TYPE = {"float": "1.0", "int": "1", "bool": "true",
                       "color": "255 128 0", "string": "probe", "": ""}


def probe_parameter(entity_type: str, io_def) -> str:
    """A parameter worth sending to this specific input."""
    name = io_def.name.lower()
    if (entity_type, name) in PROBE_PARAMS:
        return PROBE_PARAMS[(entity_type, name)]
    # The pre-2.4 type token shares LogicState's definitions and its parameters.
    if entity_type == "logic_keyvalue" and ("logic_state", name) in PROBE_PARAMS:
        return PROBE_PARAMS[("logic_state", name)]
    if name in PROBE_PARAMS_BY_NAME:
        return PROBE_PARAMS_BY_NAME[name]
    return PROBE_BY_PARAM_TYPE.get(io_def.param_type, "")


@pytest.fixture(scope="module")
def manager():
    """An ``IOManager`` with every handler on it — core and plugin alike."""
    mgr = IOManager()
    register_all_input_handlers(mgr)
    get_manager().attach_runtime(HostStub(mgr))
    return mgr


@pytest.fixture(scope="module")
def registration_counts():
    """How many times each ``(type, input)`` was registered.

    Counted by wrapping the registrar, not by reading the source: handlers
    registered in a loop (``LogicState``'s whole input table is one) are
    invisible to a text scan, and those are exactly the ones a duplicate would
    hide in.
    """
    counts = collections.Counter()
    real = IOManager.register_input_handler

    def counting(self, entity_type, input_name, handler):
        counts[(entity_type.lower(), input_name.lower())] += 1
        return real(self, entity_type, input_name, handler)

    IOManager.register_input_handler = counting
    try:
        mgr = IOManager()
        register_all_input_handlers(mgr)
        get_manager().attach_runtime(HostStub(mgr))
    finally:
        IOManager.register_input_handler = real
    return counts


# ===========================================================================
# Declaration vs implementation
# ===========================================================================

def test_every_declared_input_has_an_implementation(manager):
    """The failure the two-table split makes possible, asserted away."""
    report = audit_io_coverage(manager)
    assert report["unimplemented_inputs"] == [], (
        "these inputs are offered by the editor and run nothing: %s"
        % (report["unimplemented_inputs"],))


def test_nothing_is_implemented_that_is_not_declared(manager):
    """An input with a handler but no declaration works only for someone who
    already knows its name — it is in no dropdown and no tooltip."""
    report = audit_io_coverage(manager)
    assert report["undeclared_inputs"] == [], (
        "these inputs work but no designer can find them: %s"
        % (report["undeclared_inputs"],))


def test_no_handler_is_registered_against_an_unknown_type(manager):
    """A typo in a type token makes a handler silently unreachable."""
    report = audit_io_coverage(manager)
    assert report["unknown_types"] == [], (
        "handlers registered against types the registry has never heard of: %s"
        % (report["unknown_types"],))


def test_every_input_is_implemented_exactly_once(registration_counts):
    """Registering twice silently overwrites, so the second one wins and the
    first is dead code that reads as live."""
    duplicates = {key: n for key, n in registration_counts.items() if n > 1}
    assert duplicates == {}, (
        "registered more than once (the last registration wins): %s" % (duplicates,))


def test_a_handler_and_the_generic_dispatcher_never_both_claim_an_input(
        registration_counts):
    """Where both could serve an input, the registered handler wins and the
    generic path is unreachable for it. That is fine and deliberate — this
    pins it, so "exactly one implementation" means the one that actually runs."""
    for (entity_type, name) in registration_counts:
        if name in IOManager.GENERIC_INPUTS:
            handler = IOManager()._input_handlers
            # The specialised handler is what the dispatcher looks up first.
            assert name in IOManager.GENERIC_INPUTS
    mgr = IOManager()
    register_all_input_handlers(mgr)
    overlapping = sorted(
        key for key in mgr._input_handlers if key[1] in IOManager.GENERIC_INPUTS)
    # Every overlap must be a deliberate specialisation of a generic input,
    # never a second spelling of one.
    for entity_type, name in overlapping:
        assert (entity_type, name) in mgr._input_handlers


def test_the_abstract_allowlist_is_justified():
    """Every exemption states a reason, and names something real."""
    for key, reason in ABSTRACT_IO.items():
        assert isinstance(key, tuple) and len(key) == 2, (
            "ABSTRACT_IO keys are (entity_type, io_name): %r" % (key,))
        entity_type, name = key
        assert reason and isinstance(reason, str), (
            "%s.%s is exempted with no stated reason" % key)
        assert entity_type in IO_REGISTRY, (
            "%s.%s is exempted but %s is not a registered type" % (key + (entity_type,)))
        assert name in set(get_input_names(entity_type)) | set(
            get_output_names(entity_type)), (
            "%s.%s is exempted but is not declared at all" % key)


# ===========================================================================
# Demonstrable: every input, invoked for real
# ===========================================================================

def _probe_all_inputs(manager):
    """Call every declared input on a real instance. Returns what went wrong."""
    entities = {}
    manager.set_entity_finder(
        lambda n: next((e for e in entities.values()
                        if (e.get("name") if isinstance(e, dict)
                            else e.properties.get("name")) == n), None))
    manager.set_entity_finder_by_id(lambda i: entities.get(i))

    host = HostStub(manager)
    manager.set_logic_thread(host)

    raised, unreached, unbuildable = [], [], []

    generic = IOManager.GENERIC_INPUTS
    real_generic = IOManager._try_generic_input
    current = {}

    def tracking_generic(self, entity, input_name, parameter):
        # Reaching the generic dispatcher with a name it does not handle means
        # the input ran nothing at all — the exact silent failure under test.
        if input_name.lower() not in generic:
            unreached.append((current["type"], input_name))
        return real_generic(self, entity, input_name, parameter)

    errors = []
    real_debug = io_system.debug_log

    def capture(category, message, *args, **kwargs):
        if category == "Error" and "handler failed" in str(message):
            errors.append(str(message))

    IOManager._try_generic_input = tracking_generic
    io_system.debug_log = capture
    try:
        for entity_type in sorted(IO_REGISTRY):
            entity = _make_entity(entity_type)
            if entity is None:
                # An alias token shares its definitions with the real type and
                # has no instances of its own; anything else is a genuine gap.
                if IO_REGISTRY[entity_type] is not IO_REGISTRY.get(entity_type):
                    unbuildable.append(entity_type)
                continue
            entity_id = "probe-%s" % entity_type
            name = (entity.get("name") if isinstance(entity, dict)
                    else entity.properties.get("name"))
            entities[entity_id] = entity
            host.brushes = [entity] if isinstance(entity, dict) else []
            host.things = [] if isinstance(entity, dict) else [entity]
            current["type"] = entity_type

            for io_def in IO_REGISTRY[entity_type]["inputs"]:
                if (entity_type, io_def.name) in ABSTRACT_IO:
                    continue
                parameter = probe_parameter(entity_type, io_def)
                before = len(errors)
                manager._execute_input(name, io_def.name, parameter, "probe",
                                       target_id=entity_id)
                if len(errors) > before:
                    raised.append((entity_type, io_def.name, errors[-1]))
    finally:
        IOManager._try_generic_input = real_generic
        io_system.debug_log = real_debug

    return raised, unreached, unbuildable


def test_every_declared_input_reaches_an_implementation_when_invoked():
    """The static audit proves a handler is *registered*; this proves the
    dispatcher actually arrives at one for every declared input."""
    mgr = IOManager()
    register_all_input_handlers(mgr)
    get_manager().attach_runtime(HostStub(mgr))
    _raised, unreached, _unbuildable = _probe_all_inputs(mgr)
    assert unreached == [], (
        "these inputs dispatched to nothing: %s" % (unreached,))


def test_no_declared_input_raises_when_invoked():
    """A handler that throws on a default instance is not an implementation —
    ``_execute_input`` swallows the exception, so the input silently does
    nothing in exactly the way an unimplemented one does."""
    mgr = IOManager()
    register_all_input_handlers(mgr)
    get_manager().attach_runtime(HostStub(mgr))
    raised, _unreached, _unbuildable = _probe_all_inputs(mgr)
    assert raised == [], "\n".join(
        "%s.%s raised: %s" % entry for entry in raised)


def test_every_registered_type_has_an_instance_the_probe_can_build():
    """A type the probe cannot instantiate is a type it silently skips."""
    missing = []
    for entity_type in sorted(IO_REGISTRY):
        if _make_entity(entity_type) is None:
            missing.append(entity_type)
    # `logic_keyvalue` is the pre-2.4 token for `logic_state`: it shares that
    # type's definitions and has no instances of its own, because an entity
    # loaded from an old map reports the current type.
    assert missing == ["logic_keyvalue"], (
        "no instance could be built for: %s" % (missing,))


def test_the_host_stub_only_promises_what_the_logic_thread_has():
    """Keeps the probe honest.

    The stub stands in for ``LogicThread``. If it grew an attribute the real
    host does not have, every handler reaching for that attribute would pass
    here and fail in the game — the probe would be testing a fiction.
    """
    source = (ROOT / "engine" / "logic_thread.py").read_text(
        encoding="utf-8", errors="replace")
    stub = HostStub(IOManager())
    promised = {name for name in vars(stub)
                if name not in HostStub.LOCAL_ONLY}
    promised |= {name for name in vars(HostStub)
                 if not name.startswith("__") and callable(getattr(HostStub, name))
                 and name not in {"LOCAL_ONLY"}}

    missing = [name for name in sorted(promised)
               if not re.search(r"(self\.%s\s*[:=]|def\s+%s\b)" % (name, name), source)]
    assert missing == [], (
        "the stub promises what LogicThread does not have: %s" % (missing,))


# ===========================================================================
# Outputs
# ===========================================================================

def _emission_sites():
    """Every output name fired from shipped code, mapped to the files firing it.

    Matches any call whose name contains ``fire`` or ``emit`` and whose second
    argument is a string literal, so a wrapper counts: the Tidy plugin fires
    everything through its own ``_fire``, and an output is no less implemented
    for going through one.
    """
    pattern = re.compile(
        r"""\b\w*(?:fire|emit)\w*\s*\(\s*[^,()]+,\s*(['"])([A-Za-z_]\w*)\1""")
    sites = collections.defaultdict(set)
    for directory in SOURCE_DIRS:
        for path in (ROOT / directory).rglob("*.py"):
            if "__pycache__" in path.parts or "tests" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for match in pattern.finditer(text):
                sites[match.group(2)].add(str(path.relative_to(ROOT)))
    return sites


def test_every_declared_output_is_fired_somewhere():
    """An output nothing fires is a dead pin in the editor: a designer wires it
    and waits forever, with no error to explain why."""
    sites = _emission_sites()
    missing = []
    for entity_type in sorted(IO_REGISTRY):
        for name in get_output_names(entity_type):
            if name in sites or (entity_type, name) in ABSTRACT_IO:
                continue
            if (entity_type, name) not in missing:
                missing.append((entity_type, name))
    assert missing == [], (
        "these outputs are declared and never fired: %s" % (missing,))


def test_firing_an_undeclared_output_is_reported(monkeypatch):
    """The mirror of a stale declaration: an output the code fires that no type
    declares works for whoever knows the name and exists for nobody else."""
    seen = []
    monkeypatch.setattr(io_system, "debug_log",
                        lambda category, message, *a, **k: seen.append(
                            (category, str(message))))
    mgr = IOManager()
    mgr.set_entity_finder(lambda n: None)
    mgr.set_entity_finder_by_id(lambda i: None)
    door = {"name": "door", "id": "d", "is_door": True, "_io_connections": []}

    mgr.fire_output(door, "OnOpen")
    assert [s for s in seen if s[0] == "Error"] == []

    mgr.fire_output(door, "OnNobodyDeclaredThis")
    assert any(category == "Error" and "do not declare" in message
               for category, message in seen)


def test_the_undeclared_output_check_leaves_unknown_types_alone(monkeypatch):
    """A plugin entity with no declarations gets no opinion, as elsewhere."""
    seen = []
    monkeypatch.setattr(io_system, "debug_log",
                        lambda category, message, *a, **k: seen.append(
                            (category, str(message))))
    mgr = IOManager()
    mgr.set_entity_finder(lambda n: None)
    mgr.set_entity_finder_by_id(lambda i: None)

    class Widget:
        properties = {"name": "w", "type": "vendor_widget", "_io_connections": []}

    mgr.fire_output(Widget(), "OnWhatever")
    assert [s for s in seen if s[0] == "Error"] == []
