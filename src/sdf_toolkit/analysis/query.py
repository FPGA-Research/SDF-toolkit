"""Filter and query SDF files by various criteria."""

import re

from sdf_toolkit.core.model import (
    Corner,
    Device,
    EdgeSelect,
    Entry,
    EntryType,
    Interconnect,
    Iopath,
    MissingDelayError,
    Port,
    SDFFile,
)


def query(
    sdf: SDFFile,
    *,
    cell_types: list[str] | None = None,
    instances: list[str] | None = None,
    entry_types: list[EntryType] | None = None,
    pin_pattern: str | None = None,
    min_delay: float | None = None,
    max_delay: float | None = None,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
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
        Regex pattern searched in every port the entry names.
    min_delay : float | None
        Minimum delay threshold (inclusive). Only delay entries with a value
        at *corner* pass a threshold.
    max_delay : float | None
        Maximum delay threshold (inclusive), with the same restriction.
    corner : Corner
        Process corner for the threshold.
    edge : EdgeSelect
        How the rvalues reduce to one number for the threshold.

    Returns
    -------
    SDFFile
        A new SDFFile with the same header and the matching entries.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.query import query
    >>> from sdf_toolkit.core.model import DelayValues, PortSpec, Values
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
    >>> result = query(sdf, cell_types=["BUF"])
    >>> list(result.cells.keys())
    ['BUF']
    >>> "INV" in result.cells
    False
    """
    result = SDFFile(header=sdf.header, cells={})

    for cell_type, instances_dict in sdf.cells.items():
        if cell_types is not None and cell_type not in cell_types:
            continue

        for instance, entries in instances_dict.items():
            if instances is not None and instance not in instances:
                continue

            for entry in entries:
                if not _entry_matches(
                    entry,
                    entry_types=entry_types,
                    pin_pattern=pin_pattern,
                    min_delay=min_delay,
                    max_delay=max_delay,
                    corner=corner,
                    edge=edge,
                ):
                    continue

                # Entries are frozen, so the filtered file shares them.
                result.cells.setdefault(cell_type, {}).setdefault(instance, []).append(
                    entry
                )

    return result


def _entry_matches(
    entry: Entry,
    *,
    entry_types: list[EntryType] | None,
    pin_pattern: str | None,
    min_delay: float | None,
    max_delay: float | None,
    corner: Corner,
    edge: EdgeSelect,
) -> bool:
    """Check whether a single entry passes all filter criteria.

    Parameters
    ----------
    entry : Entry
        The entry to check.
    entry_types : list[EntryType] | None
        Allowed entry types, or None to allow all.
    pin_pattern : str | None
        Regex pattern searched in every port the entry names.
    min_delay : float | None
        Minimum delay threshold (inclusive).
    max_delay : float | None
        Maximum delay threshold (inclusive).
    corner : Corner
        Process corner for the threshold.
    edge : EdgeSelect
        How the rvalues reduce to one number for the threshold.

    Returns
    -------
    bool
        True if the entry matches all specified filters.
    """
    if entry_types is not None and entry.type not in entry_types:
        return False

    if pin_pattern is not None and not any(
        re.search(pin_pattern, pin.port) for pin in entry.key.pins
    ):
        return False

    if min_delay is not None or max_delay is not None:
        if not isinstance(entry, Iopath | Port | Interconnect | Device):
            return False
        try:
            scalar = entry.delay.scalar(corner=corner, edge=edge)
        except MissingDelayError:
            return False
        if min_delay is not None and scalar < min_delay:
            return False
        if max_delay is not None and scalar > max_delay:
            return False

    return True
