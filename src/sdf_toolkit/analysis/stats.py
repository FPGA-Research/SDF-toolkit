"""Aggregate statistics over SDF delay values."""

import statistics
from dataclasses import dataclass

from sdf_toolkit.core.model import (
    Corner,
    Device,
    EdgeSelect,
    Interconnect,
    Iopath,
    MissingDelayError,
    Port,
    SDFFile,
)


@dataclass
class SDFStats:
    """Aggregate statistics for an SDF file.

    Attributes
    ----------
    total_cells : int
        Number of unique cell types.
    total_instances : int
        Total number of instances across all cell types.
    total_entries : int
        Total number of timing entries.
    entry_type_counts : dict[str, int]
        Count of entries by entry type.
    delay_min : float | None
        Minimum delay over the delay entries that carry a value.
    delay_max : float | None
        Maximum delay over the delay entries that carry a value.
    delay_mean : float | None
        Mean delay over the delay entries that carry a value.
    delay_median : float | None
        Median delay over the delay entries that carry a value.
    """

    total_cells: int
    total_instances: int
    total_entries: int
    entry_type_counts: dict[str, int]
    delay_min: float | None
    delay_max: float | None
    delay_mean: float | None
    delay_median: float | None


def compute_stats(
    sdf: SDFFile,
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> SDFStats:
    """Compute aggregate statistics over delay values in an SDF file.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to analyze.
    corner : Corner
        Process corner to read.
    edge : EdgeSelect
        How the rvalues of each delay reduce to one number. Timing checks
        carry limits, not delays, so they count as entries only.

    Returns
    -------
    SDFStats
        Aggregate statistics.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.stats import compute_stats
    >>> from sdf_toolkit.core.model import Corner, DelayValues, PortSpec, Values
    >>> sdf = (
    ...     SDFBuilder()
    ...     .add_cell("BUF", "b0")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_cell("INV", "i0")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=4.0, typ=5.0, max=6.0)),
    ...     )
    ...     .build()
    ... )
    >>> stats = compute_stats(sdf, corner=Corner.MAX)
    >>> stats.total_cells
    2
    >>> stats.total_instances
    2
    >>> stats.delay_min
    3.0
    >>> stats.delay_max
    6.0
    """
    entry_type_counts: dict[str, int] = {}
    scalars: list[float] = []
    total_instances = 0
    total_entries = 0

    for instances in sdf.cells.values():
        total_instances += len(instances)
        for entries in instances.values():
            total_entries += len(entries)
            for entry in entries:
                type_key = str(entry.type)
                entry_type_counts[type_key] = entry_type_counts.get(type_key, 0) + 1

                if isinstance(entry, Iopath | Port | Interconnect | Device):
                    try:
                        scalars.append(entry.delay.scalar(corner=corner, edge=edge))
                    except MissingDelayError:
                        continue

    return SDFStats(
        total_cells=len(sdf.cells),
        total_instances=total_instances,
        total_entries=total_entries,
        entry_type_counts=entry_type_counts,
        delay_min=min(scalars) if scalars else None,
        delay_max=max(scalars) if scalars else None,
        delay_mean=statistics.mean(scalars) if scalars else None,
        delay_median=statistics.median(scalars) if scalars else None,
    )
