"""DELAY and pulse-limit entries, with the port and condition types every entry shares.

Each entry kind is its own frozen dataclass with only the fields its SDF
construct has. {attr}`Iopath.key` and its siblings return an {class}`ArcKey`
built from the construct itself, so merge and diff match entries by the arc
they time rather than by a generated name.
"""

from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from sdf_toolkit.core.enums import DelayMode, EdgeType, EntryType
from sdf_toolkit.core.values import DelayValues, Number, Values, _require_non_negative


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
class Retain:
    """An IOPATH ``RETAIN``: how long the output holds its old value.

    *delay* is a delval list whose positions are output transitions, as in
    the IOPATH delay, so a delval may carry pulse limits.  OVI SDF 3.0 sets
    no count, so the model takes any delval list and the parser applies the
    IEEE 1497 limit of 1 to 3.
    """

    delay: DelayValues

    def map(self, fn: Callable[[Decimal], Number]) -> "Retain":
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


DelayEntry = Iopath | Port | Interconnect | Device | NetDelay


PulseEntry = PathPulse | PathPulsePercent
