"""Filter and query SDF files by various criteria."""

import copy
import re

from sdf_toolkit.core.model import BaseEntry, DelayField, DelayFieldLike, DelayMetric, DelayMetricLike, EntryType, SDFFile


def query(
    sdf: SDFFile,
    cell_types: list[str] | None = None,
    instances: list[str] | None = None,
    entry_types: list[EntryType] | None = None,
    pin_pattern: str | None = None,
    min_delay: float | None = None,
    max_delay: float | None = None,
    field: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
) -> SDFFile:
    """Filter cells and entries by various criteria.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to filter.
    cell_types : list[str] | None
        If given, only include cells with these cell types.
    instances : list[str] | None
        If given, only include these instance names.
    entry_types : list[EntryType] | None
        If given, only include entries of these types.
    pin_pattern : str | None
        Regex pattern to match against from_pin or to_pin.
    min_delay : float | None
        Minimum delay threshold (inclusive).
    max_delay : float | None
        Maximum delay threshold (inclusive).
    field : str
        Delay field for scalar extraction.
    metric : str
        Metric for scalar extraction.

    Returns
    -------
    SDFFile
        A new SDFFile with header deep-copied and cells filtered.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.query import query
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("BUF", "b0")
    ...         .add_iopath("A", "Y", {
    ...             "nominal": {"min": 1.0, "avg": 2.0, "max": 3.0},
    ...         })
    ...     .add_cell("INV", "i0")
    ...         .add_iopath("A", "Y", {
    ...             "nominal": {"min": 4.0, "avg": 5.0, "max": 6.0},
    ...         })
    ...     .build()
    ... )
    >>> result = query(sdf, cell_types=["BUF"])
    >>> list(result.cells.keys())
    ['BUF']
    >>> "INV" in result.cells
    False
    """
    result = SDFFile(header=copy.deepcopy(sdf.header), cells={})

    for cell_type, instances_dict in sdf.cells.items():
        if cell_types is not None and cell_type not in cell_types:
            continue

        for instance, entries in instances_dict.items():
            if instances is not None and instance not in instances:
                continue

            for entry_name, entry in entries.items():
                if not _entry_matches(
                    entry,
                    entry_types=entry_types,
                    pin_pattern=pin_pattern,
                    min_delay=min_delay,
                    max_delay=max_delay,
                    field=field,
                    metric=metric,
                ):
                    continue

                result.cells.setdefault(cell_type, {}).setdefault(instance, {})[
                    entry_name
                ] = copy.deepcopy(entry)

    return result


def _entry_matches(
    entry: BaseEntry,
    *,
    entry_types: list[EntryType] | None,
    pin_pattern: str | None,
    min_delay: float | None,
    max_delay: float | None,
    field: DelayFieldLike,
    metric: DelayMetricLike,
) -> bool:
    """Check whether a single entry passes all filter criteria.

    Parameters
    ----------
    entry : BaseEntry
        The entry to check.
    entry_types : list[EntryType] | None
        Allowed entry types, or None to allow all.
    pin_pattern : str | None
        Regex pattern to match against from_pin or to_pin.
    min_delay : float | None
        Minimum delay threshold (inclusive).
    max_delay : float | None
        Maximum delay threshold (inclusive).
    field : str
        Delay field for scalar extraction.
    metric : str
        Metric for scalar extraction.

    Returns
    -------
    bool
        True if the entry matches all specified filters.
    """
    if entry_types is not None and entry.type not in entry_types:
        return False

    if pin_pattern is not None:
        from_match = re.search(pin_pattern, entry.from_pin or "")
        to_match = re.search(pin_pattern, entry.to_pin or "")
        if not from_match and not to_match:
            return False

    if min_delay is not None or max_delay is not None:
        if entry.delay_paths is None:
            return False
        scalar = entry.delay_paths.get_scalar(field, metric)
        if scalar is None:
            return False
        if min_delay is not None and scalar < min_delay:
            return False
        if max_delay is not None and scalar > max_delay:
            return False

    return True
