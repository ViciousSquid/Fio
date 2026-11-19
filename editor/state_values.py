"""Value types for :class:`editor.things.LogicState`.

Fio's state primitive used to hold nothing but strings.  Strings are fine for
storage and hopeless for arithmetic and comparison: ``"10" < "9"`` is true
lexicographically and false in every sense a level designer means.  This module
is the small type system that fixes that, and it is deliberately *only* a type
system — it stores nothing, watches nothing and knows nothing about entities.

Six types, no more::

    string  int  float  bool  null  uuid

``uuid`` is a string that happens to be a canonical UUID.  It is a distinct
*type* because UUIDs are Fio's object identity and state that references an
object should say so, but its *storage* is the plain string, so a UUID value
costs nothing extra to serialise and never needs converting back.

Nothing here imports Qt, the editor or the engine, which is the point: the
semantics of Fio's state — what a value is, when two values are equal, what
``Increment`` does to a legacy string — are testable on a bare Python install.

Backwards compatibility
-----------------------
Legacy stores are string-only, and loading one must not rewrite it: values
arrive from disk exactly as they were written (see :func:`load_store`).  What
changes is that comparison and arithmetic *understand* those strings, so a map
authored against the old store behaves the same or better, never worse.
"""

import math
import re
import sys

#: Every state type. ``uuid`` is stored as its canonical string.
STATE_TYPES = ("string", "int", "float", "bool", "null", "uuid")

#: The comparison operators :func:`compare` accepts.  Two-character operators
#: come first so a parser scanning this tuple never matches ``>`` inside ``>=``.
OPERATORS = (">=", "<=", "==", "!=", ">", "<")

#: The arithmetic operations :func:`arithmetic` accepts, lowercase.
ARITHMETIC_OPS = ("add", "subtract", "multiply", "divide", "min", "max")

_UUID_RE = re.compile(
    r"\A[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z")

_INT_RE = re.compile(r"\A[+-]?\d+\Z")
_FLOAT_RE = re.compile(r"\A[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?\Z")

#: The integers a store holds: the signed 64-bit range.  Python's integers are
#: unbounded, and an unbounded counter is a way to make a map unsaveable --
#: past 4300 digits ``json.dumps`` (and ``str``) refuse the number outright,
#: and long before that no other reader of the file can hold it.  An operation
#: whose result would leave this range is refused (see :func:`storable`).
INT_MIN = -(2 ** 63)
INT_MAX = 2 ** 63 - 1

#: Longest integer literal worth handing to ``int``: 19 digits reach
#: :data:`INT_MAX`, and the slack is for a sign and leading zeros that a
#: zero-padded number may carry.  Anything longer is out of range (or is not
#: read as a number at all) without being converted first.
_INT_LITERAL_DIGITS = 64

_TRUE_WORDS = frozenset(("true", "yes", "on"))
_FALSE_WORDS = frozenset(("false", "no", "off"))
_NULL_WORDS = frozenset(("null", "none"))


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def type_of(value) -> str:
    """The state type of an already-stored *value*.

    ``bool`` is checked before ``int`` because Python's ``bool`` *is* an
    ``int``; getting that order wrong would report every flag as an integer.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str) and _UUID_RE.match(value):
        return "uuid"
    return "string"


def storable(value) -> bool:
    """Whether *value* is something a store may hold.

    Two kinds of number are not: a float that is not finite (``inf``, ``nan``),
    and an integer outside :data:`INT_MIN`..:data:`INT_MAX`.  Neither survives
    being saved -- ``NaN`` and ``Infinity`` are not JSON, and an integer of a
    few thousand digits cannot be written at all -- and ``nan`` has a second
    vice: it is not equal to itself, so a store holding one reports a change
    on every write for ever.  Everything else (strings, bools, ``None``,
    in-range numbers) is storable.
    """
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return INT_MIN <= value <= INT_MAX
    if isinstance(value, float):
        return math.isfinite(value)
    return True


def _literal(text):
    """The number a numeric literal denotes, or ``None`` if *text* is not one.

    Never raises and never builds a huge integer: an integer literal too long
    to be in range is reported as a float (``inf`` if need be), which
    :func:`storable` then refuses like any other out-of-range number.
    """
    token = text.strip()
    if _INT_RE.match(token):
        if len(token) <= _INT_LITERAL_DIGITS:
            return int(token)
        digits = token.lstrip("+-").lstrip("0")
        if len(digits) <= 19:
            return int(token)
        return float("-inf") if token.startswith("-") else float("inf")
    if _FLOAT_RE.match(token):
        try:
            return float(token)
        except (ValueError, OverflowError):   # pragma: no cover
            return None
    return None


def out_of_range(raw) -> bool:
    """Whether *raw* denotes a number no store can hold.

    True for a non-finite float, an over-range integer, and for text that is a
    numeric literal with such a value (``"1e999"`` is infinity; fifty digits
    is beyond 64 bits).  This is the question a write asks before it happens:
    such a value is *refused*, through the same path as any other refused
    write, rather than stored in a form that cannot be saved.
    """
    if isinstance(raw, str):
        number = _literal(raw)
        return number is not None and not storable(number)
    return not storable(raw)


def _saturated(value):
    """*value* pulled back into the storable range (``nan`` becomes ``0``)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return max(INT_MIN, min(INT_MAX, value))
    if isinstance(value, float):
        if value != value:
            return 0
        if value in (float("inf"), float("-inf")):
            return math.copysign(sys.float_info.max, value)
    return value


def is_uuid(value) -> bool:
    """Whether *value* is a canonical UUID string."""
    return isinstance(value, str) and bool(_UUID_RE.match(value))


# ---------------------------------------------------------------------------
# Parsing and formatting
# ---------------------------------------------------------------------------

def parse(raw):
    """The value an I/O parameter denotes, typed.

    I/O parameters are strings — that is the whole of the wire format — so this
    is where ``"5"`` becomes an ``int`` and ``"true"`` becomes a ``bool``.  The
    rules are fixed and total, in this order:

    ===================  ================================
    ``true``/``false``   ``bool`` (also yes/no, on/off)
    ``null``/``none``    ``None``
    an integer literal   ``int``
    a float literal      ``float``
    anything else        ``str``, unchanged
    ===================  ================================

    Matching is case-insensitive and ignores surrounding whitespace, but a
    value that matches nothing is returned *exactly* as given — no trimming, no
    case folding — because for a genuine string the designer's bytes are the
    value.  A value that is already typed (anything but ``str``) passes
    straight through, so parsing twice is the same as parsing once.

    A string of digits therefore becomes a number.  When that is not wanted —
    a door code of ``007``, a version of ``1.10`` — name the type explicitly
    (``coerce(raw, "string")``, or the ``key:string=007`` I/O form).

    The result is always :func:`storable`.  A literal whose value is not --
    ``1e999`` (infinity), an integer beyond 64 bits -- is not a number Fio can
    hold, so it stays the text it was written as; an already-typed number that
    is not storable comes back as text too (``inf``, ``nan``) or, for an
    over-range integer, as the nearest integer in range.  Callers that want
    such a write *refused* rather than reinterpreted ask :func:`out_of_range`
    first, which is what the I/O handlers do.
    """
    if not isinstance(raw, str):
        if storable(raw):
            return raw
        if isinstance(raw, float):
            return format_value(raw)
        return _saturated(raw)
    token = raw.strip()
    if not token:
        return raw
    lowered = token.lower()
    if lowered in _TRUE_WORDS:
        return True
    if lowered in _FALSE_WORDS:
        return False
    if lowered in _NULL_WORDS:
        return None
    number = _literal(token)
    if number is not None and storable(number):
        return number
    return raw


def coerce(raw, type_name):
    """*raw* as the named state type, or ``None`` if it cannot be.

    Used for the explicit form (``key:int=5``) and by the Property Manager's
    type column.  An unknown type name falls back to :func:`parse`, so a map
    carrying a type Fio no longer has still loads with sensible values.

    Returns a ``(value, ok)`` pair: ``ok`` is False when the text does not
    denote a value of that type at all -- or denotes a number no store can
    hold (see :func:`storable`) -- which the caller reports rather than
    guessing at.  Never raises.
    """
    name = str(type_name or "").strip().lower()
    if name not in STATE_TYPES:
        return parse(raw), True
    text = raw.strip() if isinstance(raw, str) else raw

    if name == "string":
        return (raw if isinstance(raw, str) else format_value(raw)), True
    if name == "null":
        return None, True
    if name == "bool":
        if isinstance(text, bool):
            return text, True
        if isinstance(text, (int, float)):
            return bool(text), True
        lowered = str(text).strip().lower()
        if lowered in _TRUE_WORDS:
            return True, True
        if lowered in _FALSE_WORDS:
            return False, True
        return None, False
    if name == "int":
        try:
            if isinstance(text, bool):
                return int(text), True
            if isinstance(text, float):
                value = int(text)
            elif isinstance(text, int):
                value = text
            else:
                token = str(text).strip()
                if len(token) > _INT_LITERAL_DIGITS:
                    return None, False
                value = int(token)
        except (TypeError, ValueError, OverflowError):
            return None, False
        if not storable(value):
            return None, False
        return value, True
    if name == "float":
        try:
            value = float(text)
        except (TypeError, ValueError, OverflowError):
            return None, False
        if not storable(value):
            return None, False
        return value, True
    # uuid
    if is_uuid(text):
        return str(text), True
    return None, False


def format_value(value) -> str:
    """The canonical string form of *value* — what I/O sends down the wire.

    :func:`parse` of this string returns an equal value for every type except
    a string that happens to look like a number, which is the one lossy corner
    and the reason the explicit type form exists.
    """
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, int):
        # str() of an integer past a few thousand digits raises; nothing in a
        # store is ever that large, and nothing else should stop a format.
        return str(value if storable(value) else _saturated(value))
    if isinstance(value, str):
        return value
    return str(value)


def split_typed_key(key):
    """``"hp:int"`` -> ``("hp", "int")``; ``"hp"`` -> ``("hp", None)``.

    The optional type annotation on a key in an I/O parameter.  A colon
    followed by something that is not a state type is part of the key, so a key
    genuinely containing a colon keeps working.
    """
    text = str(key)
    if ":" not in text:
        return text.strip(), None
    head, _, tail = text.rpartition(":")
    if tail.strip().lower() in STATE_TYPES:
        return head.strip(), tail.strip().lower()
    return text.strip(), None


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------

def _as_number(value):
    """*value* as a float when it is meaningfully numeric, else ``None``."""
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        token = value.strip()
        if _INT_RE.match(token) or _FLOAT_RE.match(token):
            try:
                return float(token)
            except ValueError:      # pragma: no cover
                return None
    return None


def to_number(value, default=0):
    """*value* as a number for arithmetic, falling back to *default*.

    Integers stay integers so ``Increment`` on a counter never quietly turns it
    into a float — the single most visible way a state system can start
    printing ``5.0`` where a designer wrote ``5``.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        number = _literal(value)
        if number is not None:
            return number
    return default


def compare(actual, expected, operator) -> bool:
    """Whether ``actual <operator> expected`` holds.

    One rule decides which comparison runs, and it is the same rule in both
    directions: if *both* sides are meaningfully numeric the comparison is
    numeric, otherwise it is a comparison of their canonical strings.  So a
    legacy store holding ``"10"`` still answers ``>= 9`` correctly, and a
    stored ``True`` still equals the text ``"true"`` a connection sends.

    ``null`` participates in ``==`` and ``!=`` only; an ordered comparison
    against it is False rather than an error, because a missing-ish value is
    neither greater nor smaller than anything.
    """
    op = str(operator).strip()
    if op not in OPERATORS:
        return False

    if actual is None or expected is None:
        same = (actual is None and expected is None)
        if op == "==":
            return same
        if op == "!=":
            return not same
        return False

    a, b = _as_number(actual), _as_number(expected)
    if a is None or b is None:
        a, b = format_value(actual), format_value(expected)

    if op == "==":
        return a == b
    if op == "!=":
        return a != b
    if op == ">":
        return a > b
    if op == "<":
        return a < b
    if op == ">=":
        return a >= b
    return a <= b


def split_comparison(text):
    """``"hp>=5"`` -> ``("hp", ">=", "5")``, or ``None`` if there is no operator.

    Scans :data:`OPERATORS` in order, so ``>=`` is never mistaken for ``>``
    followed by a value starting with ``=``.
    """
    raw = str(text)
    for op in OPERATORS:
        index = raw.find(op)
        if index >= 0:
            return raw[:index].strip(), op, raw[index + len(op):].strip()
    return None


# ---------------------------------------------------------------------------
# Arithmetic
# ---------------------------------------------------------------------------

def arithmetic(current, operation, operand):
    """Apply one arithmetic *operation* to *current*, returning ``(value, ok)``.

    ``ok`` is False only when the operation cannot be defined — division by
    zero, an unknown operation, or a result (or operand) that is not a number
    a store can hold: infinity, ``nan``, an integer beyond 64 bits (see
    :func:`storable`) — in which case the caller leaves the stored value
    untouched and reports the write as refused.  So a counter multiplied in a
    loop stops at the edge of the range instead of growing until the map can
    no longer be saved.  Integer inputs give integer results for everything
    except ``divide``, which is always a float so that ``7 / 2`` is ``3.5``
    and not silently ``3``.  Never raises.
    """
    op = str(operation).strip().lower()
    a = to_number(current, 0)
    b = to_number(operand, 0)
    if not storable(a) or not storable(b):
        return current, False

    try:
        if op == "add":
            result = a + b
        elif op == "subtract":
            result = a - b
        elif op == "multiply":
            result = a * b
        elif op == "divide":
            if b == 0:
                return current, False
            result = float(a) / float(b)
        elif op == "min":
            result = min(a, b)
        elif op == "max":
            result = max(a, b)
        else:
            return current, False
    except (OverflowError, ValueError, ZeroDivisionError):
        return current, False
    if not storable(result):
        return current, False
    return result, True


def clamp(current, low, high):
    """*current* confined to ``[low, high]``, as a number.

    A reversed range is not an error: the bounds are swapped, because a
    designer who types the limits the wrong way round means the interval
    between them either way.  A bound that is not a storable number
    (``1e999``) is read as the furthest storable number in its direction, so
    "no upper limit" written that way means what it says and the result is
    always storable.
    """
    value = _saturated(to_number(current, 0))
    lo = _saturated(to_number(low, 0))
    hi = _saturated(to_number(high, 0))
    if lo > hi:
        lo, hi = hi, lo
    if value < lo:
        return lo
    if value > hi:
        return hi
    return value


def toggled(current):
    """The logical inverse of *current*, as a ``bool``.

    Anything can be toggled: numbers by truthiness, the words ``true``/``false``
    by their meaning, a missing value (``None``) to ``True`` — so a flag that
    has never been set toggles *on* first, which is the only useful answer.
    """
    if current is None:
        return True
    if isinstance(current, bool):
        return not current
    if isinstance(current, (int, float)):
        return not bool(current)
    text = str(current).strip().lower()
    if text in _TRUE_WORDS:
        return False
    if text in _FALSE_WORDS:
        return True
    return not bool(text)


# ---------------------------------------------------------------------------
# Store serialisation
# ---------------------------------------------------------------------------

#: Values a store may hold on disk.  Anything else is stringified on save,
#: which is lossless for the six state types and defined for everything else.
_JSON_TYPES = (str, int, float, bool)


def save_store(data: dict) -> dict:
    """A store's values as a deterministic, JSON-native dict.

    Deterministic means two things, both of which matter for a file under
    version control: keys come out in sorted order, and a given value always
    serialises to the same JSON.  The six state types are already JSON-native
    (a UUID is its string), so nothing is encoded, tagged or wrapped — a saved
    store is readable as exactly what it is.
    """
    out = {}
    for key in sorted(data, key=str):
        value = data[key]
        if value is None or isinstance(value, _JSON_TYPES):
            out[str(key)] = _json_safe(value)
        else:
            out[str(key)] = format_value(value)
    return out


def _json_safe(value):
    """*value* in a form strict JSON can carry: see :func:`storable`.

    The write paths refuse an unstorable number, so this only ever changes
    data that reached a store some other way (a hand-edited file, a plugin
    writing the dict directly): a non-finite float becomes its text
    (``"inf"``, ``"nan"``) and an over-range integer the nearest one in range.
    """
    if storable(value):
        return value
    if isinstance(value, float):
        return format_value(value)
    return _saturated(value)


def load_store(data) -> dict:
    """A store's values as loaded, with legacy data left exactly as it was.

    Nothing is re-parsed here, and that is deliberate.  A legacy store holds
    ``"5"``; if loading turned it into ``5`` the file would change the first
    time it was saved, and a map's data would differ depending on how many
    times it had been opened.  Strings stay strings, and :func:`compare` and
    :func:`to_number` understand them — which is why the old data never needs
    migrating in the first place.
    """
    if not isinstance(data, dict):
        return {}
    out = {}
    for key, value in data.items():
        if value is None or isinstance(value, _JSON_TYPES):
            # Unchanged for everything a store can hold; only a number that
            # could never be saved again is replaced.
            out[str(key)] = _json_safe(value)
        else:
            out[str(key)] = format_value(value)
    return out
