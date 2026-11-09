"""Timescale-aware delay normalization for SDF files."""

from collections.abc import Callable
from dataclasses import replace
from decimal import Decimal

from sdf_toolkit.core.checks import (
    BidirectSkew,
    Hold,
    NoChange,
    Period,
    Recovery,
    Recrem,
    Removal,
    Setup,
    SetupHold,
    Skew,
    Width,
)
from sdf_toolkit.core.delays import (
    Device,
    Interconnect,
    Iopath,
    NetDelay,
    PathPulse,
    PathPulsePercent,
    Port,
)
from sdf_toolkit.core.sdffile import Entry, Label, SDFFile
from sdf_toolkit.core.timingenv import (
    Arrival,
    Departure,
    DiffConstraint,
    PathConstraint,
    PeriodConstraint,
    SkewConstraint,
    Slack,
    SumConstraint,
    Waveform,
)
from sdf_toolkit.core.values import Timescale


def normalize_delays(sdf: SDFFile, target: Timescale) -> SDFFile:
    """Return a copy of *sdf* with every value rescaled to *target*.

    A file without a TIMESCALE is in the IEEE 1497 default of 1 ns.  Delays,
    check limits and path-constraint values all scale by the ratio of the
    source timescale to *target*, computed per value as
    ``value * source_fs / target_fs`` in exact decimal arithmetic; the ratio
    of two timescales is a power of ten, so the result is exact.

    Parameters
    ----------
    sdf : SDFFile
        The original SDF file. It is not modified.
    target : Timescale
        The timescale to rescale to.

    Returns
    -------
    SDFFile
        A new SDFFile whose header names *target* as its timescale.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.transform.normalize import normalize_delays
    >>> from sdf_toolkit.core.values import DelayValues, Timescale, Values
    >>> from sdf_toolkit.core.delays import PortSpec
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale=Timescale.parse("1ps"))
    ...     .add_cell("BUF", "b0")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .build()
    ... )
    >>> result = normalize_delays(sdf, Timescale.parse("1ns"))
    >>> str(result.header.timescale)
    '1ns'
    >>> result.cells["BUF"]["b0"][0].delay.rvalues[0].max
    Decimal('0.003')
    """
    source_fs = sdf.header.effective_timescale.femtoseconds
    target_fs = target.femtoseconds

    def rescale(value: Decimal) -> Decimal:
        return value * source_fs / target_fs

    return SDFFile(
        header=replace(sdf.header, timescale=target),
        cells={
            cell_type: {
                instance: [_scaled(entry, rescale) for entry in entries]
                for instance, entries in instances.items()
            }
            for cell_type, instances in sdf.cells.items()
        },
    )


def _scaled(entry: Entry, rescale: Callable[[Decimal], Decimal]) -> Entry:
    """Return *entry* with *rescale* applied to every value."""
    match entry:
        case Iopath():
            return replace(
                entry,
                delay=entry.delay.map(rescale),
                retain=tuple(r.map(rescale) for r in entry.retain),
            )
        case Port() | Interconnect() | Device() | NetDelay() | Label():
            return replace(entry, delay=entry.delay.map(rescale))
        case PathPulse():
            return replace(
                entry,
                reject=entry.reject.map(rescale),
                error=None if entry.error is None else entry.error.map(rescale),
            )
        case PathPulsePercent():
            # Percentages of the path delay carry no time unit.
            return entry
        case Setup() | Hold() | Recovery() | Removal() | Skew() | Width() | Period():
            return replace(entry, limit=entry.limit.map(rescale))
        case SetupHold():
            return replace(
                entry, setup=entry.setup.map(rescale), hold=entry.hold.map(rescale)
            )
        case Recrem():
            return replace(
                entry,
                recovery=entry.recovery.map(rescale),
                removal=entry.removal.map(rescale),
            )
        case BidirectSkew():
            return replace(
                entry,
                first_limit=entry.first_limit.map(rescale),
                second_limit=entry.second_limit.map(rescale),
            )
        case NoChange():
            return replace(
                entry, setup=entry.setup.map(rescale), hold=entry.hold.map(rescale)
            )
        case PathConstraint():
            return replace(
                entry, rise=entry.rise.map(rescale), fall=entry.fall.map(rescale)
            )
        case SumConstraint() | DiffConstraint():
            return replace(
                entry,
                rise=entry.rise.map(rescale),
                fall=None if entry.fall is None else entry.fall.map(rescale),
            )
        case PeriodConstraint() | SkewConstraint():
            return replace(entry, limit=entry.limit.map(rescale))
        case Arrival() | Departure():
            return replace(
                entry,
                earliest_rise=entry.earliest_rise.map(rescale),
                latest_rise=entry.latest_rise.map(rescale),
                earliest_fall=entry.earliest_fall.map(rescale),
                latest_fall=entry.latest_fall.map(rescale),
            )
        case Slack():
            return replace(
                entry,
                rise_setup=entry.rise_setup.map(rescale),
                fall_setup=entry.fall_setup.map(rescale),
                rise_hold=entry.rise_hold.map(rescale),
                fall_hold=entry.fall_hold.map(rescale),
                period=None if entry.period is None else rescale(entry.period),
            )
        case Waveform():
            return replace(
                entry,
                period=rescale(entry.period),
                edges=tuple(
                    replace(e, offsets=tuple(rescale(o) for o in e.offsets))
                    for e in entry.edges
                ),
            )
