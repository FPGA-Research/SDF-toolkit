"""Exact delay numbers, the timescale, and the rvalue containers.

Every number is an exact {class}`decimal.Decimal`: the parser reads it from
the text of the file, so sums and differences of delays carry no binary
rounding.  A constructor also takes an ``int`` or a ``float``; a float becomes
the decimal its shortest ``repr`` writes, so ``Values(min=0.1)`` holds
``Decimal("0.1")``.

An SDF delay is an ordered list of rvalues. The position of an rvalue in the
list selects the signal transition it times, and each rvalue is a
``(min:typ:max)`` triple whose positions select the process corner. The model
keeps both axes explicit: {class}`DelayValues` holds the rvalues in file order
and {class}`Values` holds one triple, so a consumer chooses a transition and a
corner instead of reading a field whose meaning depends on the rvalue count.
"""

import operator
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal

from sdf_toolkit.core.enums import Corner, EdgeSelect, TimeUnit, Transition
from sdf_toolkit.errors import SDFToolkitError


class SDFModelError(SDFToolkitError, ValueError):
    """A value breaks an IEEE 1497 rule the model checks on construction."""


class MissingDelayError(SDFToolkitError, ValueError):
    """No delay value exists for the requested corner and edge selection."""


class UnalignableDelayError(SDFModelError):
    """Two delays share no rvalue form, since only one gives the X transitions."""


_FS_PER_UNIT: dict[TimeUnit, int] = {
    TimeUnit.S: 10**15,
    TimeUnit.MS: 10**12,
    TimeUnit.US: 10**9,
    TimeUnit.NS: 10**6,
    TimeUnit.PS: 10**3,
    TimeUnit.FS: 1,
}


_TIMESCALE_VALUES = (1, 10, 100)


_TIMESCALE_RE = re.compile(r"([0-9]+(?:\.[0-9]*)?) *([a-z]+)")


Number = Decimal | int | float
"""A number a model constructor accepts; it is stored as a {class}`~decimal.Decimal`."""


def to_decimal(value: Number) -> Decimal:
    """Return *value* as an exact decimal.

    A float becomes the decimal its shortest ``repr`` writes, which is the
    number a literal such as ``0.1`` was written as.

    Raises
    ------
    TypeError
        If *value* is not a Decimal, int or float, or is a bool.

    Examples
    --------
    >>> to_decimal(0.1)
    Decimal('0.1')
    >>> to_decimal(3)
    Decimal('3')
    """
    match value:
        case bool():
            msg = f"An SDF number cannot be the bool {value!r}"
            raise TypeError(msg)
        case Decimal():
            return value
        case int():
            return Decimal(value)
        case float():
            return Decimal(repr(value))
        case _:
            msg = f"An SDF number is a Decimal, int or float, got {value!r}"
            raise TypeError(msg)


def _optional_decimal(value: Number | None) -> Decimal | None:
    return None if value is None else to_decimal(value)


# Python's float repr switches to exponent form outside these decimal-point
# positions, and format_number follows it so written files keep their text.
_REPR_FIXED_POINTS = range(-3, 17)


def format_number(value: Decimal) -> str:
    """Write *value* as Python writes the float of the same value.

    Trailing zeros go, a whole number keeps one decimal place, and very
    large or small magnitudes use exponent form, so a value read from text
    with at most 15 significant digits is written as ``repr(float(text))``.
    Digits past what a float holds are kept.

    Examples
    --------
    >>> format_number(Decimal("0.050"))
    '0.05'
    >>> format_number(Decimal("3"))
    '3.0'
    >>> format_number(Decimal("1E-5"))
    '1e-05'
    >>> format_number(Decimal("0.12345678901234567890"))
    '0.1234567890123456789'
    """
    if not value.is_finite():
        return repr(float(value))
    sign, digit_tuple, special = value.as_tuple()
    exponent = int(special)
    digits = "".join(map(str, digit_tuple)).rstrip("0")
    if not digits:
        return "-0.0" if sign else "0.0"
    exponent += len(digit_tuple) - len(digits)
    point = len(digits) + exponent
    if point in _REPR_FIXED_POINTS:
        if point <= 0:
            text = f"0.{'0' * -point}{digits}"
        elif point >= len(digits):
            text = f"{digits}{'0' * (point - len(digits))}.0"
        else:
            text = f"{digits[:point]}.{digits[point:]}"
    else:
        mantissa = digits[0] if len(digits) == 1 else f"{digits[0]}.{digits[1:]}"
        text = f"{mantissa}e{point - 1:+03d}"
    return f"-{text}" if sign else text


def format_whole(value: Decimal) -> str:
    """Write *value* as an integer when it has no fractional part.

    Examples
    --------
    >>> format_whole(Decimal("1.000"))
    '1'
    >>> format_whole(Decimal("1.800"))
    '1.8'
    """
    if value.is_finite() and value == value.to_integral_value():
        return str(int(value))
    return format_number(value)


@dataclass(frozen=True, slots=True)
class Timescale:
    """An SDF ``TIMESCALE``: one of 1, 10 or 100 of a time unit.

    *value* is stored as an ``int``; a whole float or Decimal such as the
    ``1.0`` of ``1.0ns`` is taken as that integer.

    Raises
    ------
    SDFModelError
        If *value* is not 1, 10 or 100, which IEEE 1497 requires.
    """

    value: int
    unit: TimeUnit

    def __post_init__(self) -> None:
        """Reject a value the standard does not allow."""
        if isinstance(self.value, bool) or self.value not in _TIMESCALE_VALUES:
            msg = (
                f"Invalid SDF timescale value {self.value}; "
                f"use one of {_TIMESCALE_VALUES}"
            )
            raise SDFModelError(msg)
        object.__setattr__(self, "value", int(self.value))

    @classmethod
    def parse(cls, text: str) -> "Timescale":
        """Parse a timescale written as in an SDF file, such as ``1ns`` or ``10 ps``.

        Raises
        ------
        SDFModelError
            If the whole of *text* is not a number followed by a time unit.

        Examples
        --------
        >>> Timescale.parse("10 ps")
        Timescale(value=10, unit=<TimeUnit.PS: 'ps'>)
        >>> Timescale.parse("1nsGARBAGE")
        Traceback (most recent call last):
        ...
        sdf_toolkit.core.values.SDFModelError: Invalid SDF timescale '1nsGARBAGE'
        """
        match = _TIMESCALE_RE.fullmatch(text.strip())
        if match is None or match.group(2) not in TimeUnit:
            msg = f"Invalid SDF timescale {text!r}"
            raise SDFModelError(msg)
        number = Decimal(match.group(1))
        if number != number.to_integral_value():
            msg = f"Invalid SDF timescale value {match.group(1)}; use one of 1, 10, 100"
            raise SDFModelError(msg)
        return cls(value=int(number), unit=TimeUnit(match.group(2)))

    @property
    def femtoseconds(self) -> int:
        """Length of one timescale step in femtoseconds."""
        return self.value * _FS_PER_UNIT[self.unit]

    def __str__(self) -> str:
        """Write the timescale as SDF does, for example ``1ns``."""
        return f"{self.value}{self.unit}"


DEFAULT_TIMESCALE = Timescale(value=1, unit=TimeUnit.NS)


DEFAULT_TOLERANCE = Decimal("1e-9")
"""Default absolute tolerance of the ``approx_eq`` methods and of diff."""


@dataclass(frozen=True, slots=True, init=False)
class Values:
    """One SDF ``(min:typ:max)`` triple; ``None`` marks an omitted position.

    Each corner is stored as a {class}`~decimal.Decimal`, converted by
    {func}`to_decimal`.

    Examples
    --------
    >>> Values(min=0.1, typ=1, max=Decimal("0.30"))
    Values(min=Decimal('0.1'), typ=Decimal('1'), max=Decimal('0.30'))
    """

    min: Decimal | None
    typ: Decimal | None
    max: Decimal | None

    def __init__(
        self,
        min: Number | None = None,  # noqa: A002 - the SDF corner name
        typ: Number | None = None,
        max: Number | None = None,  # noqa: A002 - the SDF corner name
    ) -> None:
        """Store each given corner as a Decimal."""
        object.__setattr__(self, "min", _optional_decimal(min))
        object.__setattr__(self, "typ", _optional_decimal(typ))
        object.__setattr__(self, "max", _optional_decimal(max))

    @classmethod
    def uniform(cls, value: Number) -> "Values":
        """Return a triple with *value* at every corner, as ``(value)`` means."""
        return cls(min=value, typ=value, max=value)

    def at(self, corner: Corner) -> Decimal | None:
        """Return the value at *corner*, or None if the file omits it."""
        match corner:
            case Corner.MIN:
                return self.min
            case Corner.TYP:
                return self.typ
            case Corner.MAX:
                return self.max

    def is_empty(self) -> bool:
        """Report whether every corner is omitted, as in an SDF ``()``."""
        return self.min is None and self.typ is None and self.max is None

    def to_dict(self) -> dict[str, Decimal | None]:
        """Return the triple keyed by corner name."""
        return {"min": self.min, "typ": self.typ, "max": self.max}

    def _binop(
        self, other: "Values", op: Callable[[Decimal, Decimal], Decimal]
    ) -> "Values":
        """Apply *op* corner by corner; an omitted operand gives an omitted result."""

        def _apply(a: Decimal | None, b: Decimal | None) -> Decimal | None:
            if a is not None and b is not None:
                return op(a, b)
            return None

        return Values(
            min=_apply(self.min, other.min),
            typ=_apply(self.typ, other.typ),
            max=_apply(self.max, other.max),
        )

    def map(self, fn: Callable[[Decimal], Number]) -> "Values":
        """Apply *fn* to every present corner value."""
        return Values(
            min=fn(self.min) if self.min is not None else None,
            typ=fn(self.typ) if self.typ is not None else None,
            max=fn(self.max) if self.max is not None else None,
        )

    def __add__(self, other: "Values") -> "Values":
        """Add two triples corner by corner."""
        return self._binop(other, operator.add)

    def __sub__(self, other: "Values") -> "Values":
        """Subtract two triples corner by corner."""
        return self._binop(other, operator.sub)

    def __neg__(self) -> "Values":
        """Negate every present corner value."""
        return self.map(operator.neg)

    def __mul__(self, scalar: Decimal | int) -> "Values":
        """Scale every present corner value by *scalar*."""
        return self.map(lambda v: v * scalar)

    def __rmul__(self, scalar: Decimal | int) -> "Values":
        """Scale every present corner value by *scalar*."""
        return self.__mul__(scalar)

    def approx_eq(self, other: "Values", tolerance: Number = DEFAULT_TOLERANCE) -> bool:
        """Compare within *tolerance*; an omitted corner matches only an omitted one."""
        tolerance = to_decimal(tolerance)
        for s, o in (
            (self.min, other.min),
            (self.typ, other.typ),
            (self.max, other.max),
        ):
            if s is None and o is None:
                continue
            if s is None or o is None or abs(s - o) > tolerance:
                return False
        return True


def _require_non_negative(owner: object, field: str, values: Values | None) -> None:
    """Reject a negative corner in a field the grammar types ``value``.

    OVI SDF 3.0 p.3-28 and p.4-7 let an ``rvalue`` be negative but limit a
    ``value`` to zero or positive.

    Raises
    ------
    SDFModelError
        If any present corner of *values* is below zero.
    """
    if values is None:
        return
    corners = (values.min, values.typ, values.max)
    if any(v is not None and v < 0 for v in corners):
        triple = ":".join("" if v is None else format_whole(v) for v in corners)
        msg = (
            f"{owner} {field} ({triple}) is negative, but SDF types it as a value, "
            "which is zero or positive"
        )
        raise SDFModelError(msg)


_RVALUE_COUNTS = (1, 2, 3, 6, 12)


# OVI SDF 3.0 p.4-8 reads 4 or 5 delvals as 6, and 7 to 11 as 12, with
# trailing empty delvals.
_PADDED_COUNTS = {4: 6, 5: 6, 7: 12, 8: 12, 9: 12, 10: 12, 11: 12}


_TRANSITIONS = tuple(Transition)


# Rvalue index per transition for the 2- and 3-value forms of IEEE 1497.
_TWO_VALUE_INDEX: dict[Transition, int] = {
    Transition.T01: 0,
    Transition.T10: 1,
    Transition.T0Z: 0,
    Transition.TZ1: 0,
    Transition.T1Z: 1,
    Transition.TZ0: 1,
}


_THREE_VALUE_INDEX: dict[Transition, int] = {
    Transition.T01: 0,
    Transition.T10: 1,
    Transition.T0Z: 2,
    Transition.TZ1: 0,
    Transition.T1Z: 2,
    Transition.TZ0: 1,
}


@dataclass(frozen=True, slots=True)
class PulseLimits:
    """The pulse limits a delval ``((delay) (r-limit) (e-limit))`` adds to a delay.

    A pulse shorter than the r-limit is rejected; one shorter than the
    e-limit drives the output to X.
    """

    reject: Values
    error: Values | None = None

    def map(self, fn: Callable[[Decimal], Number]) -> "PulseLimits":
        """Apply *fn* to every present corner value of both limits."""
        return PulseLimits(
            reject=self.reject.map(fn),
            error=None if self.error is None else self.error.map(fn),
        )

    def approx_eq(
        self, other: "PulseLimits", tolerance: Number = DEFAULT_TOLERANCE
    ) -> bool:
        """Compare both limits within *tolerance*."""
        if (self.error is None) != (other.error is None):
            return False
        return self.reject.approx_eq(other.reject, tolerance) and (
            self.error is None
            or other.error is None
            or self.error.approx_eq(other.error, tolerance)
        )


@dataclass(frozen=True, slots=True)
class DelayValues:
    """An SDF delay value list: 1, 2, 3, 6 or 12 rvalues in file order.

    {meth}`padded` builds the 6- or 12-rvalue form from a shorter list, as
    the standard reads it.

    *limits* holds the pulse limits of each position in file order, None
    where a position is a plain rvalue.  The whole field is None when no
    position has limits, so a delay built from rvalues alone equals the
    same delay parsed from a file.

    Raises
    ------
    SDFModelError
        If the number of rvalues is not one IEEE 1497 defines, or *limits*
        does not have one item per rvalue or holds no limits at all.
    """

    rvalues: tuple[Values, ...]
    limits: tuple[PulseLimits | None, ...] | None = None

    def __post_init__(self) -> None:
        """Reject an rvalue count the standard does not define."""
        if len(self.rvalues) not in _RVALUE_COUNTS:
            msg = (
                f"An SDF delay takes {_RVALUE_COUNTS} rvalues, got "
                f"{len(self.rvalues)}. Build it with DelayValues.padded to fill "
                "a 4- to 11-rvalue list with trailing empty rvalues."
            )
            raise SDFModelError(msg)
        if self.limits is None:
            return
        if len(self.limits) != len(self.rvalues):
            msg = (
                f"Pulse limits have {len(self.limits)} positions for "
                f"{len(self.rvalues)} rvalues; give one per rvalue"
            )
            raise SDFModelError(msg)
        if all(limit is None for limit in self.limits):
            msg = "Pulse limits hold no limit; pass limits=None instead"
            raise SDFModelError(msg)

    @classmethod
    def of(cls, *rvalues: Values) -> "DelayValues":
        """Build a delay from rvalues given in SDF order, padded as {meth}`padded`."""
        return cls.padded(rvalues)

    @classmethod
    def padded(
        cls,
        rvalues: Sequence[Values],
        limits: Sequence[PulseLimits | None] | None = None,
    ) -> "DelayValues":
        """Build a delay, filling a 4- or 5-rvalue list to 6 and 7 to 11 to 12.

        The filled positions are empty rvalues without pulse limits.

        Raises
        ------
        SDFModelError
            If the list has more than 12 rvalues or *limits* does not have
            one item per rvalue.
        """
        fill = _PADDED_COUNTS.get(len(rvalues), len(rvalues)) - len(rvalues)
        return cls(
            rvalues=(*rvalues, *(Values() for _ in range(fill))),
            limits=None if limits is None else (*limits, *(None for _ in range(fill))),
        )

    def for_transition(self, transition: Transition) -> Values:
        """Return the rvalue that times *transition*.

        Raises
        ------
        SDFModelError
            If *transition* involves X and the delay has fewer than 12
            rvalues, since the standard leaves those to the consumer.
        """
        count = len(self.rvalues)
        if count == 12:
            return self.rvalues[_TRANSITIONS.index(transition)]
        if _TRANSITIONS.index(transition) >= 6:
            msg = f"Transition {transition} needs 12 rvalues; this delay has {count}"
            raise SDFModelError(msg)
        match count:
            case 1:
                return self.rvalues[0]
            case 2:
                return self.rvalues[_TWO_VALUE_INDEX[transition]]
            case 3:
                return self.rvalues[_THREE_VALUE_INDEX[transition]]
            case _:
                return self.rvalues[_TRANSITIONS.index(transition)]

    def scalar(
        self, *, corner: Corner = Corner.MAX, edge: EdgeSelect = EdgeSelect.WORST
    ) -> Decimal:
        """Reduce the delay to one number at *corner*, selected by *edge*.

        Raises
        ------
        MissingDelayError
            If the file gives no value for that corner and edge.
        """
        match edge:
            case EdgeSelect.RISE:
                candidates = [self.for_transition(Transition.T01).at(corner)]
            case EdgeSelect.FALL:
                candidates = [self.for_transition(Transition.T10).at(corner)]
            case EdgeSelect.WORST | EdgeSelect.BEST:
                candidates = [v.at(corner) for v in self.rvalues]
        present = [v for v in candidates if v is not None]
        if not present:
            msg = f"No {corner} delay for edge selection {edge} in {self}"
            raise MissingDelayError(msg)
        return min(present) if edge == EdgeSelect.BEST else max(present)

    def aligned(
        self, other: "DelayValues"
    ) -> tuple[tuple[Values, ...], tuple[Values, ...]]:
        """Bring two delays to one rvalue form that keeps every transition.

        Equal counts pair up directly and a 1-value delay repeats, since it
        times every transition alike.  Any other pair expands to the 6-value
        form, or to the 12-value form when either side has 12.

        Raises
        ------
        UnalignableDelayError
            If only one side has 12 rvalues, since the other side gives no
            value for the X transitions.
        """
        mine, theirs = self.rvalues, other.rvalues
        if len(mine) == len(theirs):
            return mine, theirs
        if (len(mine) == 12) != (len(theirs) == 12):
            msg = (
                f"Cannot align a {len(mine)}-rvalue delay with a "
                f"{len(theirs)}-rvalue delay; only the 12-value form gives the "
                "X transitions"
            )
            raise UnalignableDelayError(msg)
        if len(mine) == 1 and len(theirs) <= 6:
            return mine * len(theirs), theirs
        if len(theirs) == 1 and len(mine) <= 6:
            return mine, theirs * len(mine)
        width = 12 if 12 in (len(mine), len(theirs)) else 6
        transitions = _TRANSITIONS[:width]
        return (
            tuple(self.for_transition(t) for t in transitions),
            tuple(other.for_transition(t) for t in transitions),
        )

    def _require_no_limits(self, other: "DelayValues", operation: str) -> None:
        if self.limits is not None or other.limits is not None:
            msg = (
                f"Cannot {operation} delays that carry pulse limits, since the "
                "limits of the result are not defined. Drop the limits first "
                "with replace(delay, limits=None)."
            )
            raise SDFModelError(msg)

    def __add__(self, other: "DelayValues") -> "DelayValues":
        """Add two delays transition by transition.

        Raises
        ------
        SDFModelError
            If either delay carries pulse limits.
        """
        self._require_no_limits(other, "add")
        mine, theirs = self.aligned(other)
        return DelayValues(
            rvalues=tuple(a + b for a, b in zip(mine, theirs, strict=True))
        )

    def __sub__(self, other: "DelayValues") -> "DelayValues":
        """Subtract two delays transition by transition.

        Raises
        ------
        SDFModelError
            If either delay carries pulse limits.
        """
        self._require_no_limits(other, "subtract")
        mine, theirs = self.aligned(other)
        return DelayValues(
            rvalues=tuple(a - b for a, b in zip(mine, theirs, strict=True))
        )

    def map(self, fn: Callable[[Decimal], Number]) -> "DelayValues":
        """Apply *fn* to every present value, pulse limits included."""
        return DelayValues(
            rvalues=tuple(v.map(fn) for v in self.rvalues),
            limits=None
            if self.limits is None
            else tuple(None if lim is None else lim.map(fn) for lim in self.limits),
        )

    def __mul__(self, scalar: Decimal | int) -> "DelayValues":
        """Scale every rvalue and pulse limit by *scalar*."""
        return self.map(lambda v: v * scalar)

    def approx_eq(
        self, other: "DelayValues", tolerance: Number = DEFAULT_TOLERANCE
    ) -> bool:
        """Compare two delays with the same rvalue count within *tolerance*."""
        if self.limits is None or other.limits is None:
            limits_match = self.limits is None and other.limits is None
        else:
            limits_match = all(
                (a is None and b is None)
                or (a is not None and b is not None and a.approx_eq(b, tolerance))
                for a, b in zip(self.limits, other.limits, strict=True)
            )
        return (
            limits_match
            and len(self.rvalues) == len(other.rvalues)
            and all(
                a.approx_eq(b, tolerance)
                for a, b in zip(self.rvalues, other.rvalues, strict=True)
            )
        )

    def to_list(self) -> list[dict[str, Decimal | None]]:
        """Return the rvalues in file order, each keyed by corner name."""
        return [v.to_dict() for v in self.rvalues]
