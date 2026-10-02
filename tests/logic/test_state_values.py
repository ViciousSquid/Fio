"""What a Fio state value *is*: typing, comparison, arithmetic, serialisation.

:mod:`editor.state_values` is the whole of Fio's state semantics and imports
nothing — no Qt, no engine, no entity model — so these run in the headless tier
and a failure here means the meaning of a stored value has changed, not that a
machine is missing a library.

The backwards-compatibility cases are the load-bearing ones.  Stores written
before 2.4 hold strings, and the promise is that they keep working untouched:
``"10" >= 9`` is true, ``Increment`` on ``"4"`` gives ``5``, and loading a
legacy store does not silently rewrite it.
"""

import json

import pytest

from editor import state_values as sv


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------

UUID_TEXT = "550e8400-e29b-41d4-a716-446655440000"


@pytest.mark.parametrize("text,expected", [
    ("5", 5),
    ("-12", -12),
    ("+3", 3),
    ("1.5", 1.5),
    (".5", 0.5),
    ("1e3", 1000.0),
    ("true", True),
    ("TRUE", True),
    ("false", False),
    ("yes", True),
    ("off", False),
    ("null", None),
    ("none", None),
    ("hello", "hello"),
    (UUID_TEXT, UUID_TEXT),
])
def test_parse_types_an_io_parameter(text, expected):
    assert sv.parse(text) == expected
    assert type(sv.parse(text)) is type(expected)


def test_parse_leaves_a_genuine_string_byte_for_byte():
    assert sv.parse("  spaced out  ") == "  spaced out  "


def test_parse_of_an_empty_string_is_that_string():
    assert sv.parse("") == ""


def test_parsing_twice_is_the_same_as_parsing_once():
    for text in ("5", "true", "null", "hello", "1.5"):
        assert sv.parse(sv.parse(text)) == sv.parse(text)


@pytest.mark.parametrize("value,expected", [
    (None, "null"),
    (True, "bool"),
    (False, "bool"),
    (5, "int"),
    (1.5, "float"),
    (UUID_TEXT, "uuid"),
    ("hello", "string"),
])
def test_type_of_names_the_state_type(value, expected):
    assert sv.type_of(value) == expected


def test_a_bool_is_not_reported_as_an_int():
    # bool subclasses int in Python; getting the check order wrong here would
    # report every flag in every map as an integer.
    assert sv.type_of(True) == "bool"


# ---------------------------------------------------------------------------
# Explicit types
# ---------------------------------------------------------------------------

def test_an_explicit_string_type_keeps_leading_zeroes():
    assert sv.coerce("007", "string") == ("007", True)


def test_an_explicit_type_that_does_not_fit_is_refused():
    assert sv.coerce("banana", "int") == (None, False)
    assert sv.coerce("banana", "bool") == (None, False)
    assert sv.coerce("not-a-uuid", "uuid") == (None, False)


def test_an_unknown_type_name_falls_back_to_inference():
    value, ok = sv.coerce("5", "sparkly")
    assert (value, ok) == (5, True)


@pytest.mark.parametrize("key,expected", [
    ("hp:int", ("hp", "int")),
    ("flag:bool", ("flag", "bool")),
    ("hp", ("hp", None)),
    ("a:b", ("a:b", None)),           # not a type name: part of the key
    ("  hp:float ", ("hp", "float")),
])
def test_split_typed_key(key, expected):
    assert sv.split_typed_key(key) == expected


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, "null"),
    (True, "true"),
    (False, "false"),
    (5, "5"),
    (1.5, "1.5"),
    ("hello", "hello"),
])
def test_format_value_is_the_wire_form(value, expected):
    assert sv.format_value(value) == expected


@pytest.mark.parametrize("value", [None, True, False, 5, -3, 1.5, "hello", UUID_TEXT])
def test_format_then_parse_round_trips(value):
    assert sv.parse(sv.format_value(value)) == value


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("a,op,b,expected", [
    (5, ">=", 5, True),
    (5, ">", 5, False),
    (4, "<", 5, True),
    (5, "!=", 4, True),
    (5, "==", 5, True),
    (5, "<=", 4, False),
])
def test_numbers_compare_as_numbers(a, op, b, expected):
    assert sv.compare(a, b, op) is expected


def test_a_legacy_string_still_compares_numerically():
    # The whole backwards-compatibility promise in one assertion: lexically
    # "10" < "9", and that is not what a map counting to ten means.
    assert sv.compare("10", 9, ">=") is True
    assert sv.compare("10", "9", ">") is True


def test_a_stored_bool_equals_the_text_a_connection_sends():
    assert sv.compare(True, sv.parse("true"), "==") is True
    assert sv.compare(False, sv.parse("true"), "==") is False


def test_a_legacy_true_string_equals_a_typed_true():
    assert sv.compare("true", True, "==") is True


def test_strings_compare_as_strings():
    assert sv.compare("apple", "banana", "<") is True
    assert sv.compare("apple", "apple", "==") is True


def test_null_takes_part_in_equality_only():
    assert sv.compare(None, None, "==") is True
    assert sv.compare(None, 5, "==") is False
    assert sv.compare(None, 5, "!=") is True
    assert sv.compare(None, 5, "<") is False
    assert sv.compare(5, None, ">") is False


def test_an_unknown_operator_is_false_rather_than_an_error():
    assert sv.compare(5, 5, "=~") is False


@pytest.mark.parametrize("text,expected", [
    ("hp>=5", ("hp", ">=", "5")),
    ("hp>5", ("hp", ">", "5")),
    ("hp<=5", ("hp", "<=", "5")),
    ("flag==true", ("flag", "==", "true")),
    ("flag!=true", ("flag", "!=", "true")),
    ("  hp  >=  5  ", ("hp", ">=", "5")),
])
def test_split_comparison_prefers_the_two_character_operator(text, expected):
    assert sv.split_comparison(text) == expected


def test_split_comparison_returns_none_without_an_operator():
    assert sv.split_comparison("hp") is None


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("current,op,operand,expected", [
    (3, "add", 4, 7),
    (3, "subtract", 4, -1),
    (3, "multiply", 4, 12),
    (7, "divide", 2, 3.5),
    (3, "min", 4, 3),
    (3, "max", 4, 4),
])
def test_arithmetic(current, op, operand, expected):
    assert sv.arithmetic(current, op, operand) == (expected, True)


def test_integers_stay_integers():
    value, _ok = sv.arithmetic(4, "add", 1)
    assert value == 5 and isinstance(value, int) and not isinstance(value, bool)


def test_arithmetic_on_a_legacy_string_gives_a_number():
    assert sv.arithmetic("4", "add", 1) == (5, True)


def test_divide_is_always_a_float_so_seven_over_two_is_not_three():
    value, ok = sv.arithmetic(7, "divide", 2)
    assert ok and value == 3.5 and isinstance(value, float)


def test_dividing_by_zero_refuses_rather_than_raising():
    assert sv.arithmetic(7, "divide", 0) == (7, False)


def test_an_unknown_operation_refuses():
    assert sv.arithmetic(7, "exponentiate", 2) == (7, False)


def test_non_numeric_operands_count_as_zero():
    assert sv.arithmetic("banana", "add", 1) == (1, True)


@pytest.mark.parametrize("value,low,high,expected", [
    (15, 0, 10, 10),
    (-5, 0, 10, 0),
    (5, 0, 10, 5),
    (15, 10, 0, 10),        # reversed bounds mean the same interval
])
def test_clamp(value, low, high, expected):
    assert sv.clamp(value, low, high) == expected


@pytest.mark.parametrize("value,expected", [
    (None, True),           # never set toggles on
    (True, False),
    (False, True),
    (0, True),
    (1, False),
    ("true", False),
    ("false", True),
    ("", True),
])
def test_toggled(value, expected):
    assert sv.toggled(value) is expected


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------

def test_saving_a_store_sorts_its_keys():
    saved = sv.save_store({"zeta": 1, "alpha": 2, "mid": 3})
    assert list(saved) == ["alpha", "mid", "zeta"]


def test_saving_is_deterministic():
    data = {"b": True, "a": 5, "c": None, "d": 1.5, "e": "text"}
    assert json.dumps(sv.save_store(data)) == json.dumps(sv.save_store(dict(data)))


def test_saved_values_are_json_native_with_no_tagging():
    saved = sv.save_store({"n": 5, "f": 1.5, "b": True, "s": "x", "z": None})
    assert json.loads(json.dumps(saved)) == {
        "n": 5, "f": 1.5, "b": True, "s": "x", "z": None}


def test_a_uuid_is_saved_as_its_plain_string():
    assert sv.save_store({"who": UUID_TEXT}) == {"who": UUID_TEXT}


def test_loading_leaves_legacy_strings_exactly_as_they_were():
    # Re-parsing on load would change a map's data the first time it was
    # opened, so a legacy "5" stays the string "5" — and still compares and
    # increments as five.
    loaded = sv.load_store({"count": "5", "flag": "true"})
    assert loaded == {"count": "5", "flag": "true"}
    assert isinstance(loaded["count"], str)
    assert sv.compare(loaded["count"], 5, "==") is True


def test_loading_a_non_dict_gives_an_empty_store():
    assert sv.load_store(None) == {}
    assert sv.load_store([1, 2]) == {}


def test_save_load_round_trips_typed_values():
    data = {"n": 5, "f": 1.5, "b": True, "s": "x", "z": None, "u": UUID_TEXT}
    assert sv.load_store(json.loads(json.dumps(sv.save_store(data)))) == data
