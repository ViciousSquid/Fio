"""A LogicState store only ever holds numbers that can be saved.

Three ways a map (or a console command) could put an unsaveable number into a
store, each of which shipped:

* ``SetValue x=1e999`` stored ``inf``; ``x:float=nan`` stored ``nan``.  Neither
  is JSON, and ``nan != nan`` meant every later write "changed" the value, so
  ``OnValueChanged`` fired for ever.
* ``Multiply`` in a loop grew a Python integer without limit; past 4300 digits
  ``json.dumps`` refuses it and the level and the session can no longer be
  saved.
* The typed-coercion path raised ``OverflowError`` instead of refusing.

The rule now: a store holds finite floats and integers in the signed 64-bit
range.  A write that would break that is *refused* through the existing
refused-write path (``OnStoreFull``), the stored value is left alone, and
nothing raises.

The first half is the pure type system (headless).  The second half drives the
real ``LogicState`` entity through the real I/O handlers and carries the ``qt``
marker, because ``editor.things`` needs PyQt5 to import.
"""

import json
import math

import pytest

from editor import state_values as sv


# ---------------------------------------------------------------------------
# The type system (headless)
# ---------------------------------------------------------------------------

#: The range a store's integers live in: signed 64-bit.
INT_MIN, INT_MAX = -(2 ** 63), 2 ** 63 - 1


def test_the_integer_range_is_signed_64_bit():
    assert (sv.INT_MIN, sv.INT_MAX) == (INT_MIN, INT_MAX)


def _short(value):
    """A test id that never prints a 5000-digit number (``str`` refuses to)."""
    if isinstance(value, int) and not isinstance(value, bool):
        return "int_%dbits" % value.bit_length()
    text = str(value)
    return text if len(text) <= 24 else "%s..%dchars" % (text[:12], len(text))


UNSTORABLE_TEXT = ["1e999", "-1e999", "9" * 40, "-" + "9" * 40, "9" * 5000,
                   str(2 ** 63), str(-(2 ** 63) - 1)]


@pytest.mark.parametrize("value", [0, -1, INT_MAX, INT_MIN, 1.5, -0.0,
                                   1.7e308, True, None, "text", "inf"], ids=_short)
def test_ordinary_values_are_storable(value):
    assert sv.storable(value), "%r should be storable" % (value,)
    assert not sv.out_of_range(value), "%r is in range" % (value,)


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan"),
                                   INT_MAX + 1, INT_MIN - 1, 10 ** 5000], ids=_short)
def test_non_finite_and_over_range_numbers_are_not_storable(value):
    assert not sv.storable(value), "%r must not be storable" % (value,)
    assert sv.out_of_range(value)


@pytest.mark.parametrize("text", UNSTORABLE_TEXT)
def test_a_literal_denoting_an_unstorable_number_is_out_of_range(text):
    assert sv.out_of_range(text), (
        "%r... denotes a number no store can hold" % text[:24])


@pytest.mark.parametrize("text", ["5", "-5", str(INT_MAX), str(INT_MIN),
                                  "1e308", "007", "0" * 100 + "7", "abc",
                                  "nan", "inf", ""], ids=_short)
def test_ordinary_text_is_not_out_of_range(text):
    assert not sv.out_of_range(text), "%r was wrongly refused" % (text,)


@pytest.mark.parametrize("text", UNSTORABLE_TEXT)
def test_parse_never_yields_an_unstorable_number(text):
    value = sv.parse(text)
    assert sv.storable(value), (
        "parse(%r...) produced the unstorable %r" % (text[:24], value))
    assert value == text, (
        "a literal Fio cannot hold as a number stays the text it was "
        "written as; got %r" % (value,))


@pytest.mark.parametrize("value", [float("inf"), float("nan"), 2 ** 80,
                                   -(2 ** 80), 10 ** 5000], ids=_short)
def test_parse_of_an_already_typed_unstorable_number_is_storable(value):
    result = sv.parse(value)
    assert sv.storable(result), "parse(%r) -> %r" % (type(value), result)
    json.dumps(result, allow_nan=False)


def test_parse_is_unchanged_for_ordinary_literals():
    assert sv.parse("5") == 5 and type(sv.parse("5")) is int
    assert sv.parse(str(INT_MAX)) == INT_MAX
    assert sv.parse("-1.5e3") == -1500.0
    assert sv.parse("1e308") == 1e308
    assert sv.parse("0007") == 7
    assert sv.parse("0" * 100 + "7") == 7, (
        "leading zeros do not make an integer out of range")


@pytest.mark.parametrize("raw,type_name", [
    (float("inf"), "int"), (float("nan"), "int"), ("1e999", "int"),
    ("9" * 40, "int"), ("9" * 5000, "int"), (2 ** 70, "int"),
    (float("inf"), "float"), (float("nan"), "float"), ("nan", "float"),
    ("inf", "float"), ("-Infinity", "float"), ("1e999", "float"),
    (10 ** 400, "float"),
], ids=_short)
def test_coerce_refuses_an_unstorable_number_without_raising(raw, type_name):
    try:
        value, ok = sv.coerce(raw, type_name)
    except Exception as exc:                                  # noqa: BLE001
        pytest.fail("coerce(%.24r, %r) raised %s; it must refuse, not raise"
                    % (raw, type_name, type(exc).__name__))
    assert (value, ok) == (None, False), (
        "coerce(%.24r, %r) accepted %r" % (raw, type_name, value))


def test_coerce_is_unchanged_for_ordinary_values():
    assert sv.coerce("5", "int") == (5, True)
    assert sv.coerce(5.9, "int") == (5, True)
    assert sv.coerce(str(INT_MIN), "int") == (INT_MIN, True)
    assert sv.coerce("2.5", "float") == (2.5, True)
    assert sv.coerce("1e999", "string") == ("1e999", True)


@pytest.mark.parametrize("current,op,operand", [
    (INT_MAX, "add", 1),
    (INT_MIN, "subtract", 1),
    (2 ** 62, "multiply", 2),
    (10, "multiply", "9" * 40),
    (1e308, "multiply", 10),
    (1e308, "add", 1e308),
    (5, "add", "1e999"),
    (5, "subtract", "1e999"),
    (5, "max", "1e999"),
    (5, "min", "-1e999"),
    (1e308, "divide", 1e-308),
    (5, "multiply", float("nan")),
    (float("inf"), "subtract", 1),
    (float("nan"), "add", 1),
    (5, "add", "9" * 5000),
], ids=_short)
def test_arithmetic_refuses_a_result_no_store_can_hold(current, op, operand):
    try:
        value, ok = sv.arithmetic(current, op, operand)
    except Exception as exc:                                  # noqa: BLE001
        pytest.fail("arithmetic(%.24r, %r, %.24r) raised %s"
                    % (current, op, operand, type(exc).__name__))
    assert ok is False, (
        "arithmetic(%.24r, %r, %.24r) should be refused, returned %r"
        % (current, op, operand, value))
    assert value is current or value == current, (
        "a refused operation returns the value it was given")


def test_arithmetic_at_the_edges_of_the_range_still_works():
    assert sv.arithmetic(INT_MAX - 1, "add", 1) == (INT_MAX, True)
    assert sv.arithmetic(INT_MIN + 1, "subtract", 1) == (INT_MIN, True)
    assert sv.arithmetic(2 ** 62, "multiply", 1) == (2 ** 62, True)
    assert sv.arithmetic(7, "divide", 2) == (3.5, True)
    assert sv.arithmetic("10", "add", "5") == (15, True)


def test_repeated_multiplication_stops_at_the_range_instead_of_growing():
    value = 10
    for _ in range(200):
        result, ok = sv.arithmetic(value, "multiply", 10 ** 18)
        if ok:
            value = result
    assert sv.storable(value) and value.bit_length() <= 64, (
        "a counter multiplied in a loop grew to %d bits" % value.bit_length())
    json.dumps({"b": value})


def test_clamp_with_an_unbounded_limit_stays_storable():
    assert sv.clamp(5, 0, "1e999") == 5
    assert sv.clamp(5, "-1e999", "1e999") == 5
    for result in (sv.clamp(5, "1e999", "1e999"),
                   sv.clamp(float("inf"), 0, "1e999"),
                   sv.clamp(float("nan"), 0, 10),
                   sv.clamp(5, "9" * 40, "9" * 41)):
        assert sv.storable(result), "clamp produced the unstorable %r" % (result,)
    assert sv.clamp(15, 0, 10) == 10 and sv.clamp(-3, 10, 0) == 0


def test_format_value_never_raises_on_a_huge_integer():
    text = sv.format_value(10 ** 5000)
    assert text == str(INT_MAX), text
    assert sv.format_value(12) == "12" and sv.format_value(float("inf")) == "inf"


def test_a_store_is_always_strict_json_when_saved_or_loaded():
    dirty = {"a": float("nan"), "b": float("inf"), "c": float("-inf"),
             "d": 10 ** 5000, "e": -(10 ** 30), "ok": 5, "f": 1.5, "s": "x",
             "n": None, "t": True}
    for convert in (sv.save_store, sv.load_store):
        clean = convert(dirty)
        try:
            json.dumps(clean, allow_nan=False)
        except ValueError as exc:
            pytest.fail("%s left a value JSON cannot carry: %s"
                        % (convert.__name__, exc))
        assert all(sv.storable(v) for v in clean.values())
        assert {k: clean[k] for k in ("ok", "f", "s", "n", "t")} == {
            "ok": 5, "f": 1.5, "s": "x", "n": None, "t": True}, (
            "ordinary values must pass through %s untouched" % convert.__name__)
        assert (clean["a"], clean["b"], clean["c"]) == ("nan", "inf", "-inf")
        assert (clean["d"], clean["e"]) == (INT_MAX, INT_MIN)


# ---------------------------------------------------------------------------
# The real entity through the real handlers
# ---------------------------------------------------------------------------

@pytest.fixture
def bench():
    pytest.importorskip("PyQt5", reason="editor.things needs PyQt5")
    from editor.things import LogicState
    from tests.logic.test_logic_state import Bench
    LogicState._persistent_registry.clear()
    yield Bench()
    LogicState._persistent_registry.clear()


def _strict_json(store):
    return json.dumps(store.to_dict(), allow_nan=False)


@pytest.mark.qt
@pytest.mark.parametrize("parameter", [
    "x=1e999", "x=-1e999", "x:float=nan", "x:float=inf", "x:int=1e999",
    "x=" + "9" * 40, "x=" + "9" * 5000, "x:int=" + "9" * 40,
], ids=_short)
def test_setvalue_refuses_an_unstorable_number(bench, parameter):
    bench.send("SetValue", "x=3")
    bench.send("SetValue", parameter)

    assert bench.outputs() == ["OnStoreFull"], (
        "SetValue %s... should be refused through OnStoreFull and nothing "
        "else; it fired %s" % (parameter[:20], bench.outputs()))
    assert bench.store.get_value("x") == 3, (
        "a refused write must leave the stored value alone; x is now %r"
        % (bench.store.get_value("x"),))
    _strict_json(bench.store)


@pytest.mark.qt
def test_setvalue_typed_as_string_keeps_number_looking_text(bench):
    bench.send("SetValue", "code:string=1e999")
    assert bench.store.get_value("code") == "1e999"
    assert "OnValueSet" in bench.outputs()


@pytest.mark.qt
def test_setobjectvalue_refuses_an_unstorable_number(bench):
    bench.send("SetObjectValue", "hp=1e999")
    assert bench.outputs() == ["OnStoreFull"], bench.outputs()
    assert not any(isinstance(v, float) and not math.isfinite(v)
                   for v in bench.store._runtime_data.values())


@pytest.mark.qt
def test_nan_cannot_make_onvaluechanged_fire_on_every_write(bench):
    for _ in range(3):
        bench.send("SetValue", "y:float=nan")
        assert "OnValueChanged" not in bench.outputs(), (
            "NaN is never equal to itself, so storing one made every write a "
            "'change'; outputs were %s" % bench.outputs())
    ok, changed, stored = bench.store.apply_value("y", float("nan"), "float")
    assert ok is False, "apply_value accepted NaN as a float"
    ok, changed, stored = bench.store.apply_value("y", float("nan"))
    ok, changed, stored = bench.store.apply_value("y", float("nan"))
    assert not changed and sv.storable(stored), (
        "an untyped NaN write must settle to one storable value, not change "
        "on every write (changed=%r stored=%r)" % (changed, stored))
    _strict_json(bench.store)


@pytest.mark.qt
def test_multiply_in_a_loop_cannot_make_the_store_unsaveable(bench):
    bench.send("SetValue", "b=10")
    refused = 0
    for _ in range(80):
        bench.send("Multiply", "b," + "1" + "0" * 84)
        refused += bench.outputs() == ["OnStoreFull"]
    for _ in range(80):
        bench.send("Multiply", "b,1000000")
        refused += bench.outputs() == ["OnStoreFull"]

    value = bench.store.get_value("b")
    assert isinstance(value, int) and INT_MIN <= value <= INT_MAX, (
        "the counter left the 64-bit range: %d bits" % value.bit_length())
    assert refused >= 150, (
        "out-of-range multiplications should be refused through OnStoreFull; "
        "only %d of 160 were" % refused)
    try:
        _strict_json(bench.store)
    except ValueError as exc:
        pytest.fail("the store can no longer be saved: %s" % exc)


@pytest.mark.qt
@pytest.mark.parametrize("input_name,parameter", [
    ("Add", "n,1e999"), ("Subtract", "n,1e999"), ("Multiply", "n,1e999"),
    ("Increment", "n,1e999"), ("Decrement", "n,1e999"),
    ("Max", "n,1e999"), ("Min", "n,-1e999"),
    ("Add", "n," + "9" * 5000), ("Divide", "n,0"),
], ids=_short)
def test_arithmetic_inputs_refuse_and_leave_the_value_alone(
        bench, input_name, parameter):
    bench.send("SetValue", "n=5")
    bench.send(input_name, parameter)

    assert bench.outputs() == ["OnStoreFull"], (
        "%s %s... should be a refused write, fired %s"
        % (input_name, parameter[:16], bench.outputs()))
    assert bench.store.get_value("n") == 5
    _strict_json(bench.store)


@pytest.mark.qt
def test_ordinary_writes_and_arithmetic_are_unaffected(bench):
    bench.send("SetValue", "n=%d" % (INT_MAX - 1))
    bench.send("Increment", "n")
    assert bench.store.get_value("n") == INT_MAX
    assert bench.outputs() == ["OnValueSet", "OnValueChanged"]
    bench.send("SetValue", "f=1e308")
    assert bench.store.get_value("f") == 1e308
    bench.send("Clamp", "f,0,1e999")
    assert bench.store.get_value("f") == 1e308


@pytest.mark.qt
def test_a_store_loaded_with_unsaveable_numbers_can_be_saved_again(bench):
    from editor.things import LogicState
    bench.store._runtime_data.update({"a": float("nan"), "b": 10 ** 5000})
    try:
        text = _strict_json(bench.store)
    except ValueError as exc:
        pytest.fail("a store holding a NaN or a huge integer cannot be "
                    "saved: %s" % exc)
    restored = json.loads(text)["runtime_data"]
    assert restored["a"] == "nan" and restored["b"] == INT_MAX
