"""Aggregate statistics over SDF delay values."""

import statistics
from dataclasses import dataclass

from sdf_toolkit.core.model import DelayField, DelayFieldLike, DelayMetric, DelayMetricLike, SDFFile


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
        Minimum delay value across all entries.
    delay_max : float | None
        Maximum delay value across all entries.
    delay_mean : float | None
        Mean delay value across all entries.
    delay_median : float | None
        Median delay value across all entries.
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
    field: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
) -> SDFStats:
    """Compute aggregate statistics over delay values in an SDF file.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to analyze.
    field : str
        Delay field to extract (nominal, fast, slow, etc.).
    metric : str
        Metric to extract (min, avg, max).

    Returns
    -------
    SDFStats
        Aggregate statistics.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.stats import compute_stats
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("BUF", "b0")
    ...         .add_iopath("A", "Y", {
    ...             "slow": {"min": 1.0, "avg": 2.0, "max": 3.0},
    ...         })
    ...     .add_cell("INV", "i0")
    ...         .add_iopath("A", "Y", {
    ...             "slow": {"min": 4.0, "avg": 5.0, "max": 6.0},
    ...         })
    ...     .build()
    ... )
    >>> stats = compute_stats(sdf, field="slow", metric="max")
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
            for entry in entries.values():
                type_key = str(entry.type)
                entry_type_counts[type_key] = entry_type_counts.get(type_key, 0) + 1

                if entry.delay_paths is not None:
                    scalar = entry.delay_paths.get_scalar(field, metric)
                    if scalar is not None:
                        scalars.append(scalar)

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
