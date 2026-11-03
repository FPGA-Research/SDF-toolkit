"""Data model for SDF timing data, after OVI SDF 3.0 and IEEE 1497 (SDF 4.0).

An SDF delay is an ordered list of rvalues. The position of an rvalue in the
list selects the signal transition it times, and each rvalue is a
``(min:typ:max)`` triple whose positions select the process corner. The model
keeps both axes explicit: {class}`DelayValues` holds the rvalues in file order
and {class}`Values` holds one triple, so a consumer chooses a transition and a
corner instead of reading a field whose meaning depends on the rvalue count.

Each entry kind is its own frozen dataclass with only the fields its SDF
construct has. {attr}`Iopath.key` and its siblings return an {class}`ArcKey`
built from the construct itself, so merge and diff match entries by the arc
they time rather than by a generated name.
"""

import operator
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from itertools import pairwise
from typing import Any, ClassVar

from sdf_toolkit.errors import SDFToolkitError


class CaseInsensitiveStrEnum(StrEnum):
    """A ``StrEnum`` whose value lookup ignores case.

    SDF and Verilog keywords are conventionally written upper case (``IOPATH``,
    ``PORT``) while the member values here are lower case. This lets
    ``EntryType("IOPATH")`` resolve instead of raising, so CLI options that take
    these names work regardless of the case the user types.
    """

    @classmethod
    def _missing_(cls, value: object) -> "CaseInsensitiveStrEnum | None":
        """Resolve a value case-insensitively, or return None if unknown."""
        if isinstance(value, str):
            lowered = value.lower()
            for member in cls:
                if member.value == lowered:
                    return member
        return None


class EntryType(CaseInsensitiveStrEnum):
    """Types of SDF timing entries.

    Member values are lower case but lookup is case-insensitive, so the
    upper-case spellings used in SDF files resolve directly:

    >>> EntryType("IOPATH") is EntryType.IOPATH
    True
    >>> EntryType("Port") is EntryType.PORT
    True
    """

    PORT = "port"
    INTERCONNECT = "interconnect"
    IOPATH = "iopath"
    DEVICE = "device"
    NETDELAY = "netdelay"
    PATHPULSE = "pathpulse"
    PATHPULSEPERCENT = "pathpulsepercent"
    SETUP = "setup"
    HOLD = "hold"
    REMOVAL = "removal"
    RECOVERY = "recovery"
    WIDTH = "width"
    PERIOD = "period"
    SETUPHOLD = "setuphold"
    RECREM = "recrem"
    SKEW = "skew"
    NOCHANGE = "nochange"
    PATHCONSTRAINT = "pathconstraint"
    PERIODCONSTRAINT = "periodconstraint"
    SKEWCONSTRAINT = "skewconstraint"
    ARRIVAL = "arrival"
    DEPARTURE = "departure"
    SLACK = "slack"
    WAVEFORM = "waveform"
    LABEL = "label"


class EdgeType(StrEnum):
    """Port edge identifiers in SDF port specifications.

    ``posedge`` and ``negedge`` cover every rising or falling transition,
    X included; the other six name one transition each.
    """

    POSEDGE = "posedge"
    NEGEDGE = "negedge"
    E01 = "01"
    E10 = "10"
    E0Z = "0z"
    EZ1 = "z1"
    E1Z = "1z"
    EZ0 = "z0"


class Corner(StrEnum):
    """Process corner, the position inside a ``(min:typ:max)`` triple."""

    MIN = "min"
    TYP = "typ"
    MAX = "max"


class Transition(StrEnum):
    """Signal transition, in the order SDF assigns rvalues to them."""

    T01 = "01"
    T10 = "10"
    T0Z = "0z"
    TZ1 = "z1"
    T1Z = "1z"
    TZ0 = "z0"
    T0X = "0x"
    TX1 = "x1"
    T1X = "1x"
    TX0 = "x0"
    TXZ = "xz"
    TZX = "zx"


class EdgeSelect(StrEnum):
    """How {meth}`DelayValues.scalar` reduces the rvalues to one number.

    ``RISE`` reads transition 01 and ``FALL`` reads transition 10.  ``WORST``
    takes the largest and ``BEST`` the smallest value present at the corner
    across every rvalue.
    """

    RISE = "rise"
    FALL = "fall"
    WORST = "worst"
    BEST = "best"


class DelayMode(StrEnum):
    """Whether a delay replaces (ABSOLUTE) or adds to (INCREMENT) a prior one."""

    ABSOLUTE = "absolute"
    INCREMENT = "increment"


class TimeUnit(StrEnum):
    """Unit of an SDF ``TIMESCALE``."""

    S = "s"
    MS = "ms"
    US = "us"
    NS = "ns"
    PS = "ps"
    FS = "fs"


class Divider(StrEnum):
    """Hierarchy divider character of an SDF ``DIVIDER``."""

    DOT = "."
    SLASH = "/"


class HeaderField(StrEnum):
    """SDF header field names."""

    SDFVERSION = "sdfversion"
    DESIGN = "design"
    VENDOR = "vendor"
    PROGRAM = "program"
    VERSION = "version"
    DIVIDER = "divider"
    DATE = "date"
    VOLTAGE = "voltage"
    PROCESS = "process"
    TEMPERATURE = "temperature"
    TIMESCALE = "timescale"


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


def _format_number(value: float) -> str:
    """Write *value* in integer form when it has no fractional part."""
    return str(int(value)) if value == int(value) else str(value)


@dataclass(frozen=True, slots=True)
class Timescale:
    """An SDF ``TIMESCALE``: one of 1, 10 or 100 of a time unit.

    Raises
    ------
    SDFModelError
        If *value* is not 1, 10 or 100, which IEEE 1497 requires.
    """

    value: float
    unit: TimeUnit

    def __post_init__(self) -> None:
        """Reject a value the standard does not allow."""
        if self.value not in _TIMESCALE_VALUES:
            msg = (
                f"Invalid SDF timescale value {self.value}; "
                f"use one of {_TIMESCALE_VALUES}"
            )
            raise SDFModelError(msg)

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
        Timescale(value=10.0, unit=<TimeUnit.PS: 'ps'>)
        >>> Timescale.parse("1nsGARBAGE")
        Traceback (most recent call last):
        ...
        sdf_toolkit.core.model.SDFModelError: Invalid SDF timescale '1nsGARBAGE'
        """
        match = _TIMESCALE_RE.fullmatch(text.strip())
        if match is None or match.group(2) not in TimeUnit:
            msg = f"Invalid SDF timescale {text!r}"
            raise SDFModelError(msg)
        return cls(value=float(match.group(1)), unit=TimeUnit(match.group(2)))

    @property
    def femtoseconds(self) -> int:
        """Length of one timescale step in femtoseconds."""
        return int(self.value) * _FS_PER_UNIT[self.unit]

    def __str__(self) -> str:
        """Write the timescale as SDF does, for example ``1ns``."""
        return f"{_format_number(self.value)}{self.unit}"


DEFAULT_TIMESCALE = Timescale(value=1, unit=TimeUnit.NS)
DEFAULT_DIVIDER = Divider.DOT


@dataclass(frozen=True, slots=True)
class Values:
    """One SDF ``(min:typ:max)`` triple; ``None`` marks an omitted position."""

    min: float | None = None
    typ: float | None = None
    max: float | None = None

    @classmethod
    def uniform(cls, value: float) -> "Values":
        """Return a triple with *value* at every corner, as ``(value)`` means."""
        return cls(min=value, typ=value, max=value)

    def at(self, corner: Corner) -> float | None:
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

    def to_dict(self) -> dict[str, float | None]:
        """Return the triple keyed by corner name."""
        return {"min": self.min, "typ": self.typ, "max": self.max}

    def _binop(self, other: "Values", op: Callable[[float, float], float]) -> "Values":
        """Apply *op* corner by corner; an omitted operand gives an omitted result."""

        def _apply(a: float | None, b: float | None) -> float | None:
            if a is not None and b is not None:
                return op(a, b)
            return None

        return Values(
            min=_apply(self.min, other.min),
            typ=_apply(self.typ, other.typ),
            max=_apply(self.max, other.max),
        )

    def map(self, fn: Callable[[float], float]) -> "Values":
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

    def __mul__(self, scalar: float) -> "Values":
        """Scale every present corner value by *scalar*."""
        return self.map(lambda v: v * scalar)

    def __rmul__(self, scalar: float) -> "Values":
        """Scale every present corner value by *scalar*."""
        return self.__mul__(scalar)

    def approx_eq(self, other: "Values", tolerance: float = 1e-9) -> bool:
        """Compare within *tolerance*; an omitted corner matches only an omitted one."""
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
        triple = ":".join("" if v is None else _format_number(v) for v in corners)
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

    def map(self, fn: Callable[[float], float]) -> "PulseLimits":
        """Apply *fn* to every present corner value of both limits."""
        return PulseLimits(
            reject=self.reject.map(fn),
            error=None if self.error is None else self.error.map(fn),
        )

    def approx_eq(self, other: "PulseLimits", tolerance: float = 1e-9) -> bool:
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
    ) -> float:
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

    def map(self, fn: Callable[[float], float]) -> "DelayValues":
        """Apply *fn* to every present value, pulse limits included."""
        return DelayValues(
            rvalues=tuple(v.map(fn) for v in self.rvalues),
            limits=None
            if self.limits is None
            else tuple(None if lim is None else lim.map(fn) for lim in self.limits),
        )

    def __mul__(self, scalar: float) -> "DelayValues":
        """Scale every rvalue and pulse limit by *scalar*."""
        return self.map(lambda v: v * scalar)

    def approx_eq(self, other: "DelayValues", tolerance: float = 1e-9) -> bool:
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

    def to_list(self) -> list[dict[str, float | None]]:
        """Return the rvalues in file order, each keyed by corner name."""
        return [v.to_dict() for v in self.rvalues]


@dataclass(frozen=True, slots=True)
class Cond:
    """An SDF ``COND`` condition, with its optional ``QSTRING`` name."""

    equation: str
    name: str | None = None


@dataclass(frozen=True, slots=True)
class CondElse:
    """An SDF ``CONDELSE``: the arc applies when no ``COND`` of the path holds."""


@dataclass(frozen=True, slots=True)
class PortSpec:
    """A port with its optional edge identifier."""

    port: str
    edge: EdgeType | None = None


@dataclass(frozen=True, slots=True)
class TimingPortSpec:
    """A timing check port (``port_tchk``) with its own optional ``COND``."""

    port: str
    edge: EdgeType | None = None
    cond: Cond | None = None


_RETAIN_COUNTS = (1, 2, 3)


@dataclass(frozen=True, slots=True)
class Retain:
    """An IOPATH ``RETAIN``: how long the output holds its old value.

    *delay* is a delval list whose positions are output transitions, as in
    the IOPATH delay, so a delval may carry pulse limits.

    Raises
    ------
    SDFModelError
        If *delay* has other than 1, 2 or 3 delvals.  A list of 4 or 5
        delvals reads as 6, so it fails too.
    """

    delay: DelayValues

    def __post_init__(self) -> None:
        """Reject a delval count the standard does not define for RETAIN."""
        if len(self.delay.rvalues) not in _RETAIN_COUNTS:
            msg = f"RETAIN takes 1 to 3 delvals, got {len(self.delay.rvalues)}"
            raise SDFModelError(msg)

    def map(self, fn: Callable[[float], float]) -> "Retain":
        """Apply *fn* to every present value, pulse limits included."""
        return Retain(delay=self.delay.map(fn))


@dataclass(frozen=True, slots=True)
class ArcKey:
    """Identity of a timing arc within one cell instance.

    Two entries with equal keys time the same arc, so merge replaces one
    with the other and diff compares their values.  ``scond`` and ``ccond``
    are the stamp and check conditions of a SETUPHOLD or RECREM, and
    ``name`` is the ``NAME`` of a PATHCONSTRAINT or the identifier of a LABEL.
    """

    type: EntryType
    pins: tuple[PortSpec | TimingPortSpec, ...]
    cond: Cond | CondElse | None
    mode: DelayMode | None
    scond: Cond | None = None
    ccond: Cond | None = None
    name: str | None = None

    def __str__(self) -> str:
        """Write the key in an SDF-like form, for reports."""
        parts = [self.type.upper()]
        if self.name is not None:
            parts.append(f'NAME("{self.name}")')
        if self.mode is not None:
            parts.insert(0, self.mode.upper())
        match self.cond:
            case Cond(equation=equation):
                parts.append(f"COND({equation})")
            case CondElse():
                parts.append("CONDELSE")
            case None:
                pass
        for pin in self.pins:
            text = f"({pin.edge} {pin.port})" if pin.edge else pin.port
            if isinstance(pin, TimingPortSpec) and pin.cond is not None:
                text = f"(COND {pin.cond.equation} {text})"
            parts.append(text)
        if self.scond is not None:
            parts.append(f"SCOND({self.scond.equation})")
        if self.ccond is not None:
            parts.append(f"CCOND({self.ccond.equation})")
        return " ".join(parts)


# ── Delay entries ───────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class _DelayEntry:
    """Fields every ``DELAY`` entry carries."""

    mode: DelayMode
    delay: DelayValues
    cond: Cond | CondElse | None = None

    type: ClassVar[EntryType]

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """Ports of the entry in SDF order."""
        raise NotImplementedError

    @property
    def key(self) -> ArcKey:
        """Arc identity of the entry."""
        return ArcKey(type=self.type, pins=self.pins, cond=self.cond, mode=self.mode)


@dataclass(frozen=True, kw_only=True)
class Iopath(_DelayEntry):
    """An ``IOPATH`` delay from an input port to an output port.

    *retain* holds the ``RETAIN`` entries in file order.
    """

    input: PortSpec
    output: PortSpec
    retain: tuple[Retain, ...] = ()

    type: ClassVar[EntryType] = EntryType.IOPATH

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """Input then output port."""
        return (self.input, self.output)


@dataclass(frozen=True, kw_only=True)
class Port(_DelayEntry):
    """A ``PORT`` delay on an input port, with its driver left implicit."""

    port: PortSpec

    type: ClassVar[EntryType] = EntryType.PORT

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The one port."""
        return (self.port,)


@dataclass(frozen=True, kw_only=True)
class Interconnect(_DelayEntry):
    """An ``INTERCONNECT`` delay from a source port to a load port."""

    source: PortSpec
    load: PortSpec

    type: ClassVar[EntryType] = EntryType.INTERCONNECT

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """Source then load port."""
        return (self.source, self.load)


@dataclass(frozen=True, kw_only=True)
class Device(_DelayEntry):
    """A ``DEVICE`` delay; without an output port it applies to every output."""

    output: PortSpec | None

    type: ClassVar[EntryType] = EntryType.DEVICE

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The output port, if the entry names one."""
        return () if self.output is None else (self.output,)


@dataclass(frozen=True, kw_only=True)
class NetDelay(_DelayEntry):
    """A ``NETDELAY`` from every source to every load of the net *net*."""

    net: str

    type: ClassVar[EntryType] = EntryType.NETDELAY

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The net, which takes no edge."""
        return (PortSpec(port=self.net),)


# ── Pulse propagation ───────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class _PulseEntry:
    """A ``DELAY`` deltype that sets how pulses propagate on a path.

    Without *path* the entry covers every IOPATH of the cell.  It sits
    beside ``ABSOLUTE`` and ``INCREMENT`` blocks, so it has no mode.

    Raises
    ------
    SDFModelError
        If a limit is negative.
    """

    path: tuple[str, str] | None
    reject: Values
    error: Values | None = None

    type: ClassVar[EntryType]

    def __post_init__(self) -> None:
        """Reject a negative limit."""
        _require_non_negative(self.key, "reject limit", self.reject)
        _require_non_negative(self.key, "error limit", self.error)

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """Input then output port, if the entry names a path."""
        if self.path is None:
            return ()
        return tuple(PortSpec(port=p) for p in self.path)

    @property
    def key(self) -> ArcKey:
        """Arc identity of the entry."""
        return ArcKey(type=self.type, pins=self.pins, cond=None, mode=None)


@dataclass(frozen=True, kw_only=True)
class PathPulse(_PulseEntry):
    """A ``PATHPULSE``: pulse rejection and X limits as times."""

    type: ClassVar[EntryType] = EntryType.PATHPULSE


@dataclass(frozen=True, kw_only=True)
class PathPulsePercent(_PulseEntry):
    """A ``PATHPULSEPERCENT``: the limits as percentages of the path delay."""

    type: ClassVar[EntryType] = EntryType.PATHPULSEPERCENT


# ── Timing checks ───────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class _TimingCheck:
    """Fields every ``TIMINGCHECK`` entry carries."""

    type: ClassVar[EntryType]

    @property
    def pins(self) -> tuple[TimingPortSpec, ...]:
        """Ports of the check in SDF order."""
        raise NotImplementedError

    @property
    def key(self) -> ArcKey:
        """Arc identity of the check; each port keeps its own ``COND``."""
        return ArcKey(type=self.type, pins=self.pins, cond=None, mode=None)


@dataclass(frozen=True, kw_only=True)
class _TwoPortCheck(_TimingCheck):
    """A check between a data port (first in SDF) and a reference port."""

    data: TimingPortSpec
    reference: TimingPortSpec

    @property
    def pins(self) -> tuple[TimingPortSpec, ...]:
        """Data then reference port."""
        return (self.data, self.reference)


@dataclass(frozen=True, kw_only=True)
class Setup(_TwoPortCheck):
    """A ``SETUP`` check."""

    limit: Values

    type: ClassVar[EntryType] = EntryType.SETUP


@dataclass(frozen=True, kw_only=True)
class Hold(_TwoPortCheck):
    """A ``HOLD`` check."""

    limit: Values

    type: ClassVar[EntryType] = EntryType.HOLD


@dataclass(frozen=True, kw_only=True)
class Recovery(_TwoPortCheck):
    """A ``RECOVERY`` check."""

    limit: Values

    type: ClassVar[EntryType] = EntryType.RECOVERY


@dataclass(frozen=True, kw_only=True)
class Removal(_TwoPortCheck):
    """A ``REMOVAL`` check."""

    limit: Values

    type: ClassVar[EntryType] = EntryType.REMOVAL


@dataclass(frozen=True, kw_only=True)
class _CondPairCheck(_TwoPortCheck):
    """A two-limit check with the optional ``SCOND`` and ``CCOND`` of IEEE 1497.

    ``scond`` conditions the stamp event and ``ccond`` the check event.

    Raises
    ------
    SDFModelError
        If a port carries a ``COND`` beside an ``SCOND`` or ``CCOND``.
    """

    scond: Cond | None = None
    ccond: Cond | None = None

    def __post_init__(self) -> None:
        """Reject port conditions in the form that takes SCOND and CCOND."""
        has_port_cond = self.data.cond is not None or self.reference.cond is not None
        if has_port_cond and (self.scond is not None or self.ccond is not None):
            msg = (
                f"{self.type.upper()} with SCOND or CCOND takes plain ports, but "
                f"{self.data.port} or {self.reference.port} carries a COND. Move the "
                "condition into the SCOND or CCOND."
            )
            raise SDFModelError(msg)

    @property
    def key(self) -> ArcKey:
        """Arc identity of the check, its SCOND and CCOND included."""
        return ArcKey(
            type=self.type,
            pins=self.pins,
            cond=None,
            mode=None,
            scond=self.scond,
            ccond=self.ccond,
        )


@dataclass(frozen=True, kw_only=True)
class SetupHold(_CondPairCheck):
    """A ``SETUPHOLD`` check with its setup and hold limits."""

    setup: Values
    hold: Values

    type: ClassVar[EntryType] = EntryType.SETUPHOLD


@dataclass(frozen=True, kw_only=True)
class Recrem(_CondPairCheck):
    """A ``RECREM`` check with its recovery and removal limits."""

    recovery: Values
    removal: Values

    type: ClassVar[EntryType] = EntryType.RECREM


@dataclass(frozen=True, kw_only=True)
class Skew(_TimingCheck):
    """A ``SKEW`` check: *limit* is the largest delay from *stamp* to *check*.

    OVI SDF 3.0 p.3-33 makes the first port the stamp event and the second
    the check event.
    """

    stamp: TimingPortSpec
    check: TimingPortSpec
    limit: Values

    type: ClassVar[EntryType] = EntryType.SKEW

    @property
    def pins(self) -> tuple[TimingPortSpec, ...]:
        """Stamp then check event port."""
        return (self.stamp, self.check)


@dataclass(frozen=True, kw_only=True)
class NoChange(_TimingCheck):
    """A ``NOCHANGE`` check: *data* holds stable around a pulse on *control*.

    The window opens *setup* before the *control* edge and closes *hold*
    after the opposite edge (OVI SDF 3.0 p.3-35).

    Raises
    ------
    SDFModelError
        If *control* has no edge, which the standard requires.
    """

    control: TimingPortSpec
    data: TimingPortSpec
    setup: Values
    hold: Values

    type: ClassVar[EntryType] = EntryType.NOCHANGE

    def __post_init__(self) -> None:
        """Reject a control port without the edge that starts the pulse."""
        if self.control.edge is None:
            msg = (
                f"NOCHANGE control port {self.control.port} needs an edge, such "
                f"as (posedge {self.control.port})"
            )
            raise SDFModelError(msg)

    @property
    def pins(self) -> tuple[TimingPortSpec, ...]:
        """Control then data port."""
        return (self.control, self.data)


@dataclass(frozen=True, kw_only=True)
class _OnePortCheck(_TimingCheck):
    """A check on one port.

    Raises
    ------
    SDFModelError
        If *limit* is negative.
    """

    port: TimingPortSpec
    limit: Values

    def __post_init__(self) -> None:
        """Reject a negative limit."""
        _require_non_negative(self.key, "limit", self.limit)

    @property
    def pins(self) -> tuple[TimingPortSpec, ...]:
        """The one port."""
        return (self.port,)


@dataclass(frozen=True, kw_only=True)
class Width(_OnePortCheck):
    """A ``WIDTH`` check."""

    type: ClassVar[EntryType] = EntryType.WIDTH


@dataclass(frozen=True, kw_only=True)
class Period(_OnePortCheck):
    """A ``PERIOD`` check."""

    type: ClassVar[EntryType] = EntryType.PERIOD


# ── Timing environment ──────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class PathConstraint:
    """A ``PATHCONSTRAINT`` over a path through two or more *ports*.

    An empty ``(NAME)`` reads as the name ``""``.

    Raises
    ------
    SDFModelError
        If the path has fewer than two ports.
    """

    ports: tuple[str, ...]
    rise: Values
    fall: Values
    name: str | None = None

    type: ClassVar[EntryType] = EntryType.PATHCONSTRAINT

    def __post_init__(self) -> None:
        """Reject a path without a start and an end."""
        if len(self.ports) < 2:
            msg = f"PATHCONSTRAINT needs at least two ports, got {self.ports}"
            raise SDFModelError(msg)

    @property
    def key(self) -> ArcKey:
        """Arc identity of the constraint, its name included."""
        return ArcKey(
            type=self.type,
            pins=tuple(PortSpec(port=p) for p in self.ports),
            cond=None,
            mode=None,
            name=self.name,
        )


@dataclass(frozen=True, kw_only=True)
class _EnvEntry:
    """A ``TIMINGENV`` entry other than PATHCONSTRAINT."""

    type: ClassVar[EntryType]

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """Ports of the entry in SDF order."""
        raise NotImplementedError

    @property
    def key(self) -> ArcKey:
        """Arc identity of the entry."""
        return ArcKey(type=self.type, pins=self.pins, cond=None, mode=None)


@dataclass(frozen=True, kw_only=True)
class PeriodConstraint(_EnvEntry):
    """A ``PERIODCONSTRAINT`` on the clock tree from *port*.

    *exception* names the instances the constraint leaves out; an empty
    ``(INSTANCE)`` reads as ``""``, as a cell instance does.

    Raises
    ------
    SDFModelError
        If *limit* is negative.
    """

    port: str
    limit: Values
    exception: tuple[str, ...] = ()

    type: ClassVar[EntryType] = EntryType.PERIODCONSTRAINT

    def __post_init__(self) -> None:
        """Reject a negative limit."""
        _require_non_negative(self.key, "limit", self.limit)

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The clock port."""
        return (PortSpec(port=self.port),)


@dataclass(frozen=True, kw_only=True)
class SkewConstraint(_EnvEntry):
    """A ``SKEWCONSTRAINT``: the largest skew across the net from *port*.

    Raises
    ------
    SDFModelError
        If *limit* is negative.
    """

    port: PortSpec
    limit: Values

    type: ClassVar[EntryType] = EntryType.SKEWCONSTRAINT

    def __post_init__(self) -> None:
        """Reject a negative limit."""
        _require_non_negative(self.key, "limit", self.limit)

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The driving port."""
        return (self.port,)


@dataclass(frozen=True, kw_only=True)
class _EdgeEnv(_EnvEntry):
    """An ``ARRIVAL`` or ``DEPARTURE`` time at *port*.

    The four times are relative to the *reference* edge, or to the implicit
    time 0 without one, in the order OVI SDF 3.0 p.3-40 and p.3-41 give.

    Raises
    ------
    SDFModelError
        If *reference* has no edge, since the standard gives it as a
        ``port_edge``.
    """

    port: str
    earliest_rise: Values
    latest_rise: Values
    earliest_fall: Values
    latest_fall: Values
    reference: PortSpec | None = None

    def __post_init__(self) -> None:
        """Reject a reference port without its edge."""
        if self.reference is not None and self.reference.edge is None:
            msg = f"{self.type.upper()} reference {self.reference.port} needs an edge"
            raise SDFModelError(msg)

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The reference port, when present, then the timed port."""
        port = PortSpec(port=self.port)
        return (port,) if self.reference is None else (self.reference, port)


@dataclass(frozen=True, kw_only=True)
class Arrival(_EdgeEnv):
    """An ``ARRIVAL`` time at a primary input or internal port."""

    type: ClassVar[EntryType] = EntryType.ARRIVAL


@dataclass(frozen=True, kw_only=True)
class Departure(_EdgeEnv):
    """A ``DEPARTURE`` time at a primary output or internal port."""

    type: ClassVar[EntryType] = EntryType.DEPARTURE


@dataclass(frozen=True, kw_only=True)
class Slack(_EnvEntry):
    """A ``SLACK`` at *port*, with the clock *period* it was found at.

    The four slacks come in the order OVI SDF 3.0 p.3-43 gives.

    Raises
    ------
    SDFModelError
        If *period* is negative.
    """

    port: str
    rise_setup: Values
    fall_setup: Values
    rise_hold: Values
    fall_hold: Values
    period: float | None = None

    type: ClassVar[EntryType] = EntryType.SLACK

    def __post_init__(self) -> None:
        """Reject a negative clock period."""
        if self.period is not None:
            _require_non_negative(self.key, "period", Values.uniform(self.period))

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The port."""
        return (PortSpec(port=self.port),)


_WAVEFORM_EDGES = (EdgeType.POSEDGE, EdgeType.NEGEDGE)


@dataclass(frozen=True, slots=True)
class WaveformEdge:
    """One edge of a ``WAVEFORM`` with its one or two offsets in the period.

    One offset places the edge; two bound the region it falls in, start
    first.

    Raises
    ------
    SDFModelError
        If the edge is not posedge or negedge, or the offset count is not
        one or two.
    """

    edge: EdgeType
    offsets: tuple[float, ...]

    def __post_init__(self) -> None:
        """Reject an edge or offset count the standard does not define."""
        if self.edge not in _WAVEFORM_EDGES:
            msg = f"a WAVEFORM edge is posedge or negedge, got {self.edge}"
            raise SDFModelError(msg)
        if len(self.offsets) not in (1, 2):
            msg = f"a WAVEFORM edge takes 1 or 2 offsets, got {len(self.offsets)}"
            raise SDFModelError(msg)


@dataclass(frozen=True, kw_only=True)
class Waveform(_EnvEntry):
    """A ``WAVEFORM`` of *period* driven at *port*.

    Raises
    ------
    SDFModelError
        If the edges are not whole posedge and negedge pairs that alternate,
        or the offsets of the edge list, read in order, do not strictly
        increase or exceed the period (OVI SDF 3.0 p.3-44).
    """

    port: str
    period: float
    edges: tuple[WaveformEdge, ...]

    type: ClassVar[EntryType] = EntryType.WAVEFORM

    def __post_init__(self) -> None:
        """Reject edges that do not pair up or offsets out of order."""
        alternates = all(a.edge is not b.edge for a, b in pairwise(self.edges))
        if not self.edges or len(self.edges) % 2 or not alternates:
            edges = " ".join(str(e.edge) for e in self.edges)
            msg = (
                f"WAVEFORM {self.port} edges must alternate in posedge and "
                f"negedge pairs, got [{edges}]"
            )
            raise SDFModelError(msg)
        offsets = [o for e in self.edges for o in e.offsets]
        if any(b <= a for a, b in pairwise(offsets)):
            msg = (
                f"WAVEFORM {self.port} offsets must increase through the edge "
                f"list, got {offsets}"
            )
            raise SDFModelError(msg)
        if offsets[-1] > self.period:
            msg = (
                f"WAVEFORM {self.port} offset {offsets[-1]} exceeds the period "
                f"{self.period}"
            )
            raise SDFModelError(msg)

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """The port."""
        return (PortSpec(port=self.port),)


# ── Labels ──────────────────────────────────────────────────────────


@dataclass(frozen=True, kw_only=True)
class Label:
    """A ``LABEL`` value: the delay a model variable such as a specparam takes.

    *name* is the identifier of the variable, not a port.
    """

    name: str
    mode: DelayMode
    delay: DelayValues

    type: ClassVar[EntryType] = EntryType.LABEL

    @property
    def key(self) -> ArcKey:
        """Identity of the label: its variable and mode."""
        return ArcKey(
            type=self.type, pins=(), cond=None, mode=self.mode, name=self.name
        )


DelayEntry = Iopath | Port | Interconnect | Device | NetDelay
PulseEntry = PathPulse | PathPulsePercent
TwoPortCheck = Setup | Hold | Recovery | Removal | SetupHold | Recrem
TimingCheck = TwoPortCheck | Skew | NoChange | Width | Period
TimingEnvEntry = (
    PathConstraint
    | PeriodConstraint
    | SkewConstraint
    | Arrival
    | Departure
    | Slack
    | Waveform
)
Entry = DelayEntry | PulseEntry | TimingCheck | TimingEnvEntry | Label

# Cell type -> instance -> entries in file order.
CellsDict = dict[str, dict[str, list[Entry]]]


def group_by_key(entries: Iterable[Entry]) -> dict[ArcKey, list[Entry]]:
    """Group *entries* by arc, keeping file order within each group."""
    groups: dict[ArcKey, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(entry.key, []).append(entry)
    return groups


def _delay_labels(delay: DelayValues, name: str = "delay") -> list[tuple[str, Values]]:
    labels = [(f"{name}[{i}]", v) for i, v in enumerate(delay.rvalues)]
    for i, limits in enumerate(delay.limits or ()):
        if limits is not None:
            labels.append((f"{name}[{i}].reject", limits.reject))
            if limits.error is not None:
                labels.append((f"{name}[{i}].error", limits.error))
    return labels


def labelled_values(entry: Entry) -> list[tuple[str, Values]]:
    """Return every value triple of *entry*, labelled by the field it fills.

    Delay rvalues read ``delay[i]`` in file order; checks read ``limit`` or
    their two named limits; a path constraint reads ``rise`` and ``fall``.
    """
    match entry:
        case Iopath():
            return _delay_labels(entry.delay) + [
                label
                for k, retain in enumerate(entry.retain)
                for label in _delay_labels(retain.delay, f"retain[{k}]")
            ]
        case Port() | Interconnect() | Device() | NetDelay() | Label():
            return _delay_labels(entry.delay)
        case PathPulse() | PathPulsePercent():
            error = [] if entry.error is None else [("error", entry.error)]
            return [("reject", entry.reject), *error]
        case Setup() | Hold() | Recovery() | Removal() | Skew() | Width() | Period():
            return [("limit", entry.limit)]
        case SetupHold():
            return [("setup", entry.setup), ("hold", entry.hold)]
        case Recrem():
            return [("recovery", entry.recovery), ("removal", entry.removal)]
        case NoChange():
            return [("setup", entry.setup), ("hold", entry.hold)]
        case PathConstraint():
            return [("rise", entry.rise), ("fall", entry.fall)]
        case PeriodConstraint() | SkewConstraint():
            return [("limit", entry.limit)]
        case Arrival() | Departure():
            return [
                ("earliest_rise", entry.earliest_rise),
                ("latest_rise", entry.latest_rise),
                ("earliest_fall", entry.earliest_fall),
                ("latest_fall", entry.latest_fall),
            ]
        case Slack():
            return [
                ("rise_setup", entry.rise_setup),
                ("fall_setup", entry.fall_setup),
                ("rise_hold", entry.rise_hold),
                ("fall_hold", entry.fall_hold),
            ]
        case Waveform():
            # Offsets and the period are plain numbers, not triples.
            return []


# ── Header and file ─────────────────────────────────────────────────


@dataclass(frozen=True)
class SDFHeader:
    """SDF header fields as the file writes them; None marks an absent field."""

    sdfversion: str | None = None
    design: str | None = None
    vendor: str | None = None
    program: str | None = None
    version: str | None = None
    divider: Divider | None = None
    date: str | None = None
    voltage: str | None = None
    process: str | None = None
    temperature: str | None = None
    timescale: Timescale | None = None

    @property
    def effective_timescale(self) -> Timescale:
        """The timescale in force: the file's, else the 1 ns IEEE 1497 default."""
        return self.timescale if self.timescale is not None else DEFAULT_TIMESCALE

    @property
    def effective_divider(self) -> Divider:
        """The divider in force: the file's, else the ``.`` IEEE 1497 default."""
        return self.divider if self.divider is not None else DEFAULT_DIVIDER

    def merged(self, later: "SDFHeader") -> "SDFHeader":
        """Return this header with every field *later* sets taking its place."""
        return SDFHeader(
            sdfversion=_later(self.sdfversion, later.sdfversion),
            design=_later(self.design, later.design),
            vendor=_later(self.vendor, later.vendor),
            program=_later(self.program, later.program),
            version=_later(self.version, later.version),
            divider=_later(self.divider, later.divider),
            date=_later(self.date, later.date),
            voltage=_later(self.voltage, later.voltage),
            process=_later(self.process, later.process),
            temperature=_later(self.temperature, later.temperature),
            timescale=_later(self.timescale, later.timescale),
        )

    def to_dict(self) -> dict[HeaderField, str]:
        """Return the fields the file sets, written as in SDF."""
        written: dict[HeaderField, str | None] = {
            HeaderField.SDFVERSION: self.sdfversion,
            HeaderField.DESIGN: self.design,
            HeaderField.VENDOR: self.vendor,
            HeaderField.PROGRAM: self.program,
            HeaderField.VERSION: self.version,
            HeaderField.DIVIDER: None if self.divider is None else str(self.divider),
            HeaderField.DATE: self.date,
            HeaderField.VOLTAGE: self.voltage,
            HeaderField.PROCESS: self.process,
            HeaderField.TEMPERATURE: self.temperature,
            HeaderField.TIMESCALE: (
                None if self.timescale is None else str(self.timescale)
            ),
        }
        return {k: v for k, v in written.items() if v is not None}

    @classmethod
    def from_dict(
        cls, data: Mapping[HeaderField, str] | Mapping[str, str]
    ) -> "SDFHeader":
        """Rebuild a header from {meth}`to_dict` output.

        Raises
        ------
        ValueError
            If a key is not a header field, or the divider or timescale is
            not valid SDF.
        """
        fields = {HeaderField(k): v for k, v in data.items()}
        divider = fields.pop(HeaderField.DIVIDER, None)
        timescale = fields.pop(HeaderField.TIMESCALE, None)
        return cls(
            sdfversion=fields.get(HeaderField.SDFVERSION),
            design=fields.get(HeaderField.DESIGN),
            vendor=fields.get(HeaderField.VENDOR),
            program=fields.get(HeaderField.PROGRAM),
            version=fields.get(HeaderField.VERSION),
            divider=None if divider is None else Divider(divider),
            date=fields.get(HeaderField.DATE),
            voltage=fields.get(HeaderField.VOLTAGE),
            process=fields.get(HeaderField.PROCESS),
            temperature=fields.get(HeaderField.TEMPERATURE),
            timescale=None if timescale is None else Timescale.parse(timescale),
        )


def _later[T](earlier: T | None, later: T | None) -> T | None:
    """Return *later* when it is set, else *earlier*."""
    return later if later is not None else earlier


@dataclass
class SDFFile:
    """Top-level SDF file: header and the entries of every cell instance."""

    header: SDFHeader = field(default_factory=SDFHeader)
    cells: CellsDict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the file as JSON-ready nested dictionaries and lists."""
        return {
            "header": {str(k): v for k, v in self.header.to_dict().items()},
            "cells": {
                cell_type: {
                    instance: [entry_to_dict(entry) for entry in entries]
                    for instance, entries in instances.items()
                }
                for cell_type, instances in self.cells.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SDFFile":
        """Rebuild a file from {meth}`to_dict` output.

        Raises
        ------
        ValueError
            If any part of *data* does not describe a valid entry or header.
        """
        return cls(
            header=SDFHeader.from_dict(data["header"]),
            cells={
                cell_type: {
                    instance: [entry_from_dict(e) for e in entries]
                    for instance, entries in instances.items()
                }
                for cell_type, instances in data["cells"].items()
            },
        )


# ── JSON form ───────────────────────────────────────────────────────


def entry_to_dict(entry: Entry) -> dict[str, Any]:
    """Return *entry* as a JSON-ready dictionary tagged with its ``type``.

    A ``CONDELSE`` becomes an empty mapping, so it stays distinct from an
    absent condition, which becomes None.
    """
    return {"type": str(entry.type), **asdict(entry)}


def _values(data: Mapping[str, float | None]) -> Values:
    return Values(min=data["min"], typ=data["typ"], max=data["max"])


def _delay(data: Mapping[str, Any]) -> DelayValues:
    limits = data["limits"]
    return DelayValues(
        rvalues=tuple(_values(v) for v in data["rvalues"]),
        limits=None
        if limits is None
        else tuple(
            None
            if lim is None
            else PulseLimits(
                reject=_values(lim["reject"]),
                error=None if lim["error"] is None else _values(lim["error"]),
            )
            for lim in limits
        ),
    )


def _port(data: Mapping[str, Any]) -> PortSpec:
    edge = data["edge"]
    return PortSpec(port=data["port"], edge=None if edge is None else EdgeType(edge))


def _cond(data: Mapping[str, Any] | None) -> Cond | None:
    return None if data is None else Cond(equation=data["equation"], name=data["name"])


def _timing_port(data: Mapping[str, Any]) -> TimingPortSpec:
    edge = data["edge"]
    return TimingPortSpec(
        port=data["port"],
        edge=None if edge is None else EdgeType(edge),
        cond=_cond(data["cond"]),
    )


def _delay_cond(data: Mapping[str, Any] | None) -> Cond | CondElse | None:
    if data is None:
        return None
    if not data:
        return CondElse()
    return _cond(data)


def _optional_values(data: Mapping[str, float | None] | None) -> Values | None:
    return None if data is None else _values(data)


def _pulse_path(data: list[str] | None) -> tuple[str, str] | None:
    if data is None:
        return None
    source, sink = data
    return (source, sink)


def entry_from_dict(data: Mapping[str, Any]) -> Entry:
    """Rebuild an entry from {func}`entry_to_dict` output.

    Raises
    ------
    ValueError
        If ``type`` is unknown or a value is not valid SDF.
    KeyError
        If a field the entry type needs is missing.
    """
    entry_type = EntryType(data["type"])
    match entry_type:
        case EntryType.IOPATH:
            return Iopath(
                input=_port(data["input"]),
                output=_port(data["output"]),
                retain=tuple(Retain(delay=_delay(r["delay"])) for r in data["retain"]),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.PORT:
            return Port(
                port=_port(data["port"]),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.INTERCONNECT:
            return Interconnect(
                source=_port(data["source"]),
                load=_port(data["load"]),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.DEVICE:
            output = data["output"]
            return Device(
                output=None if output is None else _port(output),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.NETDELAY:
            return NetDelay(
                net=data["net"],
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.PATHPULSE | EntryType.PATHPULSEPERCENT:
            pulse_cls = (
                PathPulse if entry_type is EntryType.PATHPULSE else PathPulsePercent
            )
            return pulse_cls(
                path=_pulse_path(data["path"]),
                reject=_values(data["reject"]),
                error=_optional_values(data["error"]),
            )
        case EntryType.SETUP | EntryType.HOLD | EntryType.RECOVERY | EntryType.REMOVAL:
            check_cls = _LIMIT_CHECKS[entry_type]
            return check_cls(
                data=_timing_port(data["data"]),
                reference=_timing_port(data["reference"]),
                limit=_values(data["limit"]),
            )
        case EntryType.SETUPHOLD:
            return SetupHold(
                data=_timing_port(data["data"]),
                reference=_timing_port(data["reference"]),
                setup=_values(data["setup"]),
                hold=_values(data["hold"]),
                scond=_cond(data["scond"]),
                ccond=_cond(data["ccond"]),
            )
        case EntryType.RECREM:
            return Recrem(
                data=_timing_port(data["data"]),
                reference=_timing_port(data["reference"]),
                recovery=_values(data["recovery"]),
                removal=_values(data["removal"]),
                scond=_cond(data["scond"]),
                ccond=_cond(data["ccond"]),
            )
        case EntryType.SKEW:
            return Skew(
                stamp=_timing_port(data["stamp"]),
                check=_timing_port(data["check"]),
                limit=_values(data["limit"]),
            )
        case EntryType.NOCHANGE:
            return NoChange(
                control=_timing_port(data["control"]),
                data=_timing_port(data["data"]),
                setup=_values(data["setup"]),
                hold=_values(data["hold"]),
            )
        case EntryType.WIDTH:
            return Width(port=_timing_port(data["port"]), limit=_values(data["limit"]))
        case EntryType.PERIOD:
            return Period(port=_timing_port(data["port"]), limit=_values(data["limit"]))
        case EntryType.PATHCONSTRAINT:
            return PathConstraint(
                name=data["name"],
                ports=tuple(data["ports"]),
                rise=_values(data["rise"]),
                fall=_values(data["fall"]),
            )
        case EntryType.PERIODCONSTRAINT:
            return PeriodConstraint(
                port=data["port"],
                limit=_values(data["limit"]),
                exception=tuple(data["exception"]),
            )
        case EntryType.SKEWCONSTRAINT:
            return SkewConstraint(
                port=_port(data["port"]), limit=_values(data["limit"])
            )
        case EntryType.ARRIVAL | EntryType.DEPARTURE:
            env_cls = Arrival if entry_type is EntryType.ARRIVAL else Departure
            reference = data["reference"]
            return env_cls(
                reference=None if reference is None else _port(reference),
                port=data["port"],
                earliest_rise=_values(data["earliest_rise"]),
                latest_rise=_values(data["latest_rise"]),
                earliest_fall=_values(data["earliest_fall"]),
                latest_fall=_values(data["latest_fall"]),
            )
        case EntryType.SLACK:
            return Slack(
                port=data["port"],
                rise_setup=_values(data["rise_setup"]),
                fall_setup=_values(data["fall_setup"]),
                rise_hold=_values(data["rise_hold"]),
                fall_hold=_values(data["fall_hold"]),
                period=data["period"],
            )
        case EntryType.LABEL:
            return Label(
                name=data["name"],
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
            )
        case EntryType.WAVEFORM:
            return Waveform(
                port=data["port"],
                period=data["period"],
                edges=tuple(
                    WaveformEdge(edge=EdgeType(e["edge"]), offsets=tuple(e["offsets"]))
                    for e in data["edges"]
                ),
            )


_LIMIT_CHECKS: dict[EntryType, type[Setup | Hold | Recovery | Removal]] = {
    EntryType.SETUP: Setup,
    EntryType.HOLD: Hold,
    EntryType.RECOVERY: Recovery,
    EntryType.REMOVAL: Removal,
}
