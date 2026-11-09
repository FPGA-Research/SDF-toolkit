"""TIMINGCHECK entries, one frozen dataclass per check keyword."""

from dataclasses import dataclass
from typing import ClassVar

from sdf_toolkit.core.delays import ArcKey, Cond, TimingPortSpec
from sdf_toolkit.core.enums import EntryType
from sdf_toolkit.core.values import SDFModelError, Values, _require_non_negative


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
class BidirectSkew(_TimingCheck):
    """A ``BIDIRECTSKEW`` check: a ``SKEW`` that either port may start.

    *first_limit* bounds the skew when *first* transitions first, and
    *second_limit* when *second* does (P1497 Draft 0.10 5.5.11).

    Raises
    ------
    SDFModelError
        If a limit is negative.
    """

    first: TimingPortSpec
    second: TimingPortSpec
    first_limit: Values
    second_limit: Values

    type: ClassVar[EntryType] = EntryType.BIDIRECTSKEW

    def __post_init__(self) -> None:
        """Reject a negative limit."""
        _require_non_negative(self.key, "first_limit", self.first_limit)
        _require_non_negative(self.key, "second_limit", self.second_limit)

    @property
    def pins(self) -> tuple[TimingPortSpec, ...]:
        """First then second port, as written."""
        return (self.first, self.second)


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


TwoPortCheck = Setup | Hold | Recovery | Removal | SetupHold | Recrem


TimingCheck = TwoPortCheck | Skew | BidirectSkew | NoChange | Width | Period
