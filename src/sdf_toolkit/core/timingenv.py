"""TIMINGENV entries, one frozen dataclass per constraint or environment keyword."""

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import ClassVar

from sdf_toolkit.core.delays import ArcKey, PortSpec
from sdf_toolkit.core.enums import EdgeType, EntryType
from sdf_toolkit.core.values import (
    SDFModelError,
    Values,
    _optional_decimal,
    _require_non_negative,
    to_decimal,
)


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
class ConstraintPath:
    """One path of a ``SUM`` or ``DIFF``, from the port *start* to *end*."""

    start: str
    end: str


@dataclass(frozen=True, kw_only=True)
class _PathSetConstraint(_EnvEntry):
    """A bound on the delays along several paths.

    *rise* bounds them for a rising transition at the path ends and *fall*
    for a falling one.  With *fall* None, *rise* is the one value and bounds
    both (OVI SDF 3.0 p.3-38).
    """

    paths: tuple[ConstraintPath, ...]
    rise: Values
    fall: Values | None = None

    @property
    def pins(self) -> tuple[PortSpec, ...]:
        """Start then end port of each path, in file order."""
        return tuple(
            PortSpec(port=port)
            for path in self.paths
            for port in (path.start, path.end)
        )


@dataclass(frozen=True, kw_only=True)
class SumConstraint(_PathSetConstraint):
    """A ``SUM``: the delays along *paths* add up to less than the limit.

    Raises
    ------
    SDFModelError
        If there are fewer than two paths.
    """

    type: ClassVar[EntryType] = EntryType.SUM

    def __post_init__(self) -> None:
        """Reject a sum over fewer than two paths."""
        if len(self.paths) < 2:
            msg = f"SUM needs at least two paths, got {len(self.paths)}"
            raise SDFModelError(msg)


@dataclass(frozen=True, kw_only=True)
class DiffConstraint(_PathSetConstraint):
    """A ``DIFF``: the delays along two paths differ by less than the limit.

    Raises
    ------
    SDFModelError
        If there are other than two paths, or a limit is negative.
    """

    type: ClassVar[EntryType] = EntryType.DIFF

    def __post_init__(self) -> None:
        """Reject other than two paths and a negative limit."""
        if len(self.paths) != 2:
            msg = f"DIFF takes exactly two paths, got {len(self.paths)}"
            raise SDFModelError(msg)
        _require_non_negative(self.key, "rise", self.rise)
        _require_non_negative(self.key, "fall", self.fall)


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
    period: Decimal | None = None

    type: ClassVar[EntryType] = EntryType.SLACK

    def __post_init__(self) -> None:
        """Store the period as a Decimal and reject a negative one."""
        object.__setattr__(self, "period", _optional_decimal(self.period))
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
    offsets: tuple[Decimal, ...]

    def __post_init__(self) -> None:
        """Store the offsets as Decimals; reject a bad edge or offset count."""
        object.__setattr__(self, "offsets", tuple(map(to_decimal, self.offsets)))
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
    period: Decimal
    edges: tuple[WaveformEdge, ...]

    type: ClassVar[EntryType] = EntryType.WAVEFORM

    def __post_init__(self) -> None:
        """Store the period as a Decimal; reject unpaired edges or bad offsets."""
        object.__setattr__(self, "period", to_decimal(self.period))
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


TimingEnvEntry = (
    PathConstraint
    | SumConstraint
    | DiffConstraint
    | PeriodConstraint
    | SkewConstraint
    | Arrival
    | Departure
    | Slack
    | Waveform
)
