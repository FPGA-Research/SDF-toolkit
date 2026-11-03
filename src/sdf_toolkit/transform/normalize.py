"""Timescale-aware delay normalization for SDF files."""

from collections.abc import Callable
from dataclasses import replace

from sdf_toolkit.core.model import (
    Arrival,
    Departure,
    Device,
    Entry,
    Hold,
    Interconnect,
    Iopath,
    Label,
    NetDelay,
    NoChange,
    PathConstraint,
    PathPulse,
    PathPulsePercent,
    Period,
    PeriodConstraint,
    Port,
    Recovery,
    Recrem,
    Removal,
    SDFFile,
    Setup,
    SetupHold,
    Skew,
    SkewConstraint,
    Slack,
    Timescale,
    Waveform,
    Width,
)


def normalize_delays(sdf: SDFFile, target: Timescale) -> SDFFile:
    """Return a copy of *sdf* with every value rescaled to *target*.

    A file without a TIMESCALE is in the IEEE 1497 default of 1 ns.  Delays,
    check limits and path-constraint values all scale by the ratio of the
    source timescale to *target*, computed per value as
    ``value * source_fs / target_fs`` so the integer femtosecond lengths are
    not first rounded into one float factor.

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
    >>> from sdf_toolkit.core.model import DelayValues, PortSpec, Timescale, Values
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
    0.003
    """
    source_fs = sdf.header.effective_timescale.femtoseconds
    target_fs = target.femtoseconds

    def rescale(value: float) -> float:
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


def _scaled(entry: Entry, rescale: Callable[[float], float]) -> Entry:
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
        case NoChange():
            return replace(
                entry, start=entry.start.map(rescale), end=entry.end.map(rescale)
            )
        case PathConstraint():
            return replace(
                entry, rise=entry.rise.map(rescale), fall=entry.fall.map(rescale)
            )
        case PeriodConstraint() | SkewConstraint():
            return replace(entry, limit=entry.limit.map(rescale))
        case Arrival() | Departure():
            first, second, third, fourth = (v.map(rescale) for v in entry.values)
            return replace(entry, values=(first, second, third, fourth))
        case Slack():
            first, second, third, fourth = (v.map(rescale) for v in entry.values)
            return replace(
                entry,
                values=(first, second, third, fourth),
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
