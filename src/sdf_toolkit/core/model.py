"""Data model for SDF 3.0 (IEEE 1497) timing data.

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
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, ClassVar, TypeVar

_T = TypeVar("_T")


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
    SETUP = "setup"
    HOLD = "hold"
    REMOVAL = "removal"
    RECOVERY = "recovery"
    WIDTH = "width"
    PERIOD = "period"
    SETUPHOLD = "setuphold"
    PATHCONSTRAINT = "pathconstraint"


class EdgeType(StrEnum):
    """Port edge identifiers in SDF port specifications."""

    POSEDGE = "posedge"
    NEGEDGE = "negedge"


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


class MissingDelayError(ValueError):
    """No delay value exists for the requested corner and edge selection."""


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
    ValueError
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
            raise ValueError(msg)

    @classmethod
    def parse(cls, text: str) -> "Timescale":
        """Parse a timescale written as in an SDF file, such as ``1ns`` or ``10 ps``.

        Raises
        ------
        ValueError
            If the whole of *text* is not a number followed by a time unit.

        Examples
        --------
        >>> Timescale.parse("10 ps")
        Timescale(value=10.0, unit=<TimeUnit.PS: 'ps'>)
        >>> Timescale.parse("1nsGARBAGE")
        Traceback (most recent call last):
        ...
        ValueError: Invalid SDF timescale '1nsGARBAGE'
        """
        match = _TIMESCALE_RE.fullmatch(text.strip())
        if match is None or match.group(2) not in TimeUnit:
            msg = f"Invalid SDF timescale {text!r}"
            raise ValueError(msg)
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


_RVALUE_COUNTS = (1, 2, 3, 6, 12)
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
class DelayValues:
    """An SDF delay value list: 1, 2, 3, 6 or 12 rvalues in file order.

    Raises
    ------
    ValueError
        If the number of rvalues is not one IEEE 1497 defines.
    """

    rvalues: tuple[Values, ...]

    def __post_init__(self) -> None:
        """Reject an rvalue count the standard does not define."""
        if len(self.rvalues) not in _RVALUE_COUNTS:
            msg = (
                f"An SDF delay takes {_RVALUE_COUNTS} rvalues, got {len(self.rvalues)}"
            )
            raise ValueError(msg)

    @classmethod
    def of(cls, *rvalues: Values) -> "DelayValues":
        """Build a delay from rvalues given in SDF order."""
        return cls(rvalues=rvalues)

    def for_transition(self, transition: Transition) -> Values:
        """Return the rvalue that times *transition*.

        Raises
        ------
        ValueError
            If *transition* involves X and the delay has fewer than 12
            rvalues, since the standard leaves those to the consumer.
        """
        count = len(self.rvalues)
        if count == 12:
            return self.rvalues[_TRANSITIONS.index(transition)]
        if _TRANSITIONS.index(transition) >= 6:
            msg = f"Transition {transition} needs 12 rvalues; this delay has {count}"
            raise ValueError(msg)
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
        ValueError
            If only one side has 12 rvalues, since the other side gives no
            value for the X transitions.
        """
        mine, theirs = self.rvalues, other.rvalues
        if len(mine) == len(theirs):
            return mine, theirs
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

    def __add__(self, other: "DelayValues") -> "DelayValues":
        """Add two delays transition by transition."""
        mine, theirs = self.aligned(other)
        return DelayValues(
            rvalues=tuple(a + b for a, b in zip(mine, theirs, strict=True))
        )

    def __sub__(self, other: "DelayValues") -> "DelayValues":
        """Subtract two delays transition by transition."""
        mine, theirs = self.aligned(other)
        return DelayValues(
            rvalues=tuple(a - b for a, b in zip(mine, theirs, strict=True))
        )

    def __mul__(self, scalar: float) -> "DelayValues":
        """Scale every rvalue by *scalar*."""
        return DelayValues(rvalues=tuple(v * scalar for v in self.rvalues))

    def approx_eq(self, other: "DelayValues", tolerance: float = 1e-9) -> bool:
        """Compare two delays with the same rvalue count within *tolerance*."""
        return len(self.rvalues) == len(other.rvalues) and all(
            a.approx_eq(b, tolerance)
            for a, b in zip(self.rvalues, other.rvalues, strict=True)
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


@dataclass(frozen=True, slots=True)
class ArcKey:
    """Identity of a timing arc within one cell instance.

    Two entries with equal keys time the same arc, so merge replaces one
    with the other and diff compares their values.
    """

    type: EntryType
    pins: tuple[PortSpec | TimingPortSpec, ...]
    cond: Cond | CondElse | None
    mode: DelayMode | None

    def __str__(self) -> str:
        """Write the key in an SDF-like form, for reports."""
        parts = [self.type.upper()]
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
    """An ``IOPATH`` delay from an input port to an output port."""

    input: PortSpec
    output: PortSpec

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
class SetupHold(_TwoPortCheck):
    """A ``SETUPHOLD`` check with its setup and hold limits."""

    setup: Values
    hold: Values

    type: ClassVar[EntryType] = EntryType.SETUPHOLD


@dataclass(frozen=True, kw_only=True)
class _OnePortCheck(_TimingCheck):
    """A check on one port."""

    port: TimingPortSpec
    limit: Values

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
    """A ``PATHCONSTRAINT`` over a path through *ports*."""

    ports: tuple[str, ...]
    rise: Values
    fall: Values

    type: ClassVar[EntryType] = EntryType.PATHCONSTRAINT

    @property
    def key(self) -> ArcKey:
        """Arc identity of the constraint."""
        return ArcKey(
            type=self.type,
            pins=tuple(PortSpec(port=p) for p in self.ports),
            cond=None,
            mode=None,
        )


DelayEntry = Iopath | Port | Interconnect | Device
TwoPortCheck = Setup | Hold | Recovery | Removal | SetupHold
TimingCheck = TwoPortCheck | Width | Period
Entry = DelayEntry | TimingCheck | PathConstraint

# Cell type -> instance -> entries in file order.
CellsDict = dict[str, dict[str, list[Entry]]]


def group_by_key(entries: Iterable[Entry]) -> dict[ArcKey, list[Entry]]:
    """Group *entries* by arc, keeping file order within each group."""
    groups: dict[ArcKey, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(entry.key, []).append(entry)
    return groups


def labelled_values(entry: Entry) -> list[tuple[str, Values]]:
    """Return every value triple of *entry*, labelled by the field it fills.

    Delay rvalues read ``delay[i]`` in file order; checks read ``limit``,
    ``setup`` and ``hold``; a path constraint reads ``rise`` and ``fall``.
    """
    match entry:
        case Iopath() | Port() | Interconnect() | Device():
            return [(f"delay[{i}]", v) for i, v in enumerate(entry.delay.rvalues)]
        case Setup() | Hold() | Recovery() | Removal() | Width() | Period():
            return [("limit", entry.limit)]
        case SetupHold():
            return [("setup", entry.setup), ("hold", entry.hold)]
        case PathConstraint():
            return [("rise", entry.rise), ("fall", entry.fall)]


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


def _later(earlier: _T | None, later: _T | None) -> _T | None:
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
    if entry_type in (
        EntryType.IOPATH,
        EntryType.PORT,
        EntryType.INTERCONNECT,
        EntryType.DEVICE,
    ):
        mode = DelayMode(data["mode"])
        delay = DelayValues(rvalues=tuple(_values(v) for v in data["delay"]["rvalues"]))
        cond = _delay_cond(data["cond"])
        match entry_type:
            case EntryType.IOPATH:
                return Iopath(
                    input=_port(data["input"]),
                    output=_port(data["output"]),
                    mode=mode,
                    delay=delay,
                    cond=cond,
                )
            case EntryType.PORT:
                return Port(port=_port(data["port"]), mode=mode, delay=delay, cond=cond)
            case EntryType.INTERCONNECT:
                return Interconnect(
                    source=_port(data["source"]),
                    load=_port(data["load"]),
                    mode=mode,
                    delay=delay,
                    cond=cond,
                )
            case _:
                output = data["output"]
                return Device(
                    output=None if output is None else _port(output),
                    mode=mode,
                    delay=delay,
                    cond=cond,
                )
    match entry_type:
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
            )
        case EntryType.WIDTH:
            return Width(port=_timing_port(data["port"]), limit=_values(data["limit"]))
        case EntryType.PERIOD:
            return Period(port=_timing_port(data["port"]), limit=_values(data["limit"]))
        case _:
            return PathConstraint(
                ports=tuple(data["ports"]),
                rise=_values(data["rise"]),
                fall=_values(data["fall"]),
            )


_LIMIT_CHECKS: dict[EntryType, type[Setup | Hold | Recovery | Removal]] = {
    EntryType.SETUP: Setup,
    EntryType.HOLD: Hold,
    EntryType.RECOVERY: Recovery,
    EntryType.REMOVAL: Removal,
}
