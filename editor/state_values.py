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

import re

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
    """
    if not isinstance(raw, str):
        return raw
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
    if _INT_RE.match(token):
        return int(token)
    if _FLOAT_RE.match(token):
        try:
            return float(token)
        except ValueError:      # pragma: no cover - the regex already decided
            return raw
    return raw


def coerce(raw, type_name):
    """*raw* as the named state type, or ``None`` if it cannot be.

    Used for the explicit form (``key:int=5``) and by the Property Manager's
    type column.  An unknown type name falls back to :func:`parse`, so a map
    carrying a type Fio no longer has still loads with sensible values.

    Returns a ``(value, ok)`` pair: ``ok`` is False when the text does not
    denote a value of that type at all, which the caller reports rather than
    guessing at.
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
                return int(text), True
            return int(str(text).strip()), True
        except (TypeError, ValueError):
            return None, False
    if name == "float":
        try:
            return float(text), True
        except (TypeError, ValueError):
            return None, False
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
        return str(value)
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
        token = value.strip()
        if _INT_RE.match(token):
            return int(token)
        if _FLOAT_RE.match(token):
            try:
                return float(token)
            except ValueError:      # pragma: no cover
                return default
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
    zero, an unknown operation — in which case the caller leaves the stored
    value untouched and fires nothing.  Integer inputs give integer results for
    everything except ``divide``, which is always a float so that ``7 / 2`` is
    ``3.5`` and not silently ``3``.
    """
    op = str(operation).strip().lower()
    a = to_number(current, 0)
    b = to_number(operand, 0)

    if op == "add":
        return a + b, True
    if op == "subtract":
        return a - b, True
    if op == "multiply":
        return a * b, True
    if op == "divide":
        if b == 0:
            return current, False
        return float(a) / float(b), True
    if op == "min":
        return min(a, b), True
    if op == "max":
        return max(a, b), True
    return current, False


def clamp(current, low, high):
    """*current* confined to ``[low, high]``, as a number.

    A reversed range is not an error: the bounds are swapped, because a
    designer who types the limits the wrong way round means the interval
    between them either way.
    """
    value = to_number(current, 0)
    lo = to_number(low, 0)
    hi = to_number(high, 0)
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
            out[str(key)] = value
        else:
            out[str(key)] = format_value(value)
    return out


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
            out[str(key)] = value
        else:
            out[str(key)] = format_value(value)
    return out
