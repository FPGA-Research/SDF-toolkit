r"""Filter and query SDF files by various criteria.

Instance and pin filters compare against unescaped names, so ``u[0]`` selects
the SDF instance ``u\[0\]``.
"""

import re
from decimal import Decimal

from sdf_toolkit.core.identifiers import sdf_unescape
from sdf_toolkit.core.model import (
    Corner,
    Device,
    EdgeSelect,
    Entry,
    EntryType,
    Interconnect,
    Iopath,
    MissingDelayError,
    NetDelay,
    Number,
    Port,
    SDFFile,
    to_decimal,
)


def query(
    sdf: SDFFile,
    *,
    cell_types: list[str] | None = None,
    instances: list[str] | None = None,
    entry_types: list[EntryType] | None = None,
    pin_pattern: str | None = None,
    min_delay: Number | None = None,
    max_delay: Number | None = None,
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
        If given, only include these instance names, compared unescaped.
    entry_types : list[EntryType] | None
        If given, only include entries of these types.
    pin_pattern : str | None
        Regex pattern searched in the unescaped form of every port the
        entry names.  An entry that names no port does not match.
    min_delay : Number | None
        Minimum delay threshold (inclusive).  Only delay entries pass a
        threshold.
    max_delay : Number | None
        Maximum delay threshold (inclusive), with the same restriction.
    corner : Corner
        Process corner for the threshold.
    edge : EdgeSelect
        How the rvalues reduce to one number for the threshold.

    Returns
    -------
    SDFFile
        A new SDFFile with the same header and the matching entries.

    Raises
    ------
    MissingDelayError
        If a delay threshold is set and a delay entry has no value at
        *corner* for *edge*.

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
    wanted = None if instances is None else {sdf_unescape(i) for i in instances}
    pin_regex = None if pin_pattern is None else re.compile(pin_pattern)
    min_delay = None if min_delay is None else to_decimal(min_delay)
    max_delay = None if max_delay is None else to_decimal(max_delay)

    for cell_type, instances_dict in sdf.cells.items():
        if cell_types is not None and cell_type not in cell_types:
            continue

        for instance, entries in instances_dict.items():
            if wanted is not None and sdf_unescape(instance) not in wanted:
                continue

            for entry in entries:
                try:
                    matches = _entry_matches(
                        entry,
                        entry_types=entry_types,
                        pin_regex=pin_regex,
                        min_delay=min_delay,
                        max_delay=max_delay,
                        corner=corner,
                        edge=edge,
                    )
                except MissingDelayError as exc:
                    msg = f"{cell_type} {instance} {entry.key}: {exc}"
                    raise MissingDelayError(msg) from exc
                if not matches:
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
    pin_regex: re.Pattern[str] | None,
    min_delay: Decimal | None,
    max_delay: Decimal | None,
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
    pin_regex : re.Pattern[str] | None
        Pattern searched in the unescaped form of every port the entry names.
    min_delay : Decimal | None
        Minimum delay threshold (inclusive).
    max_delay : Decimal | None
        Maximum delay threshold (inclusive).
    corner : Corner
        Process corner for the threshold.
    edge : EdgeSelect
        How the rvalues reduce to one number for the threshold.

    Returns
    -------
    bool
        True if the entry matches all specified filters.

    Raises
    ------
    MissingDelayError
        If a threshold is set and the delay has no value at *corner*.
    """
    if entry_types is not None and entry.type not in entry_types:
        return False

    if pin_regex is not None and not any(
        pin_regex.search(sdf_unescape(pin.port)) for pin in entry.key.pins
    ):
        return False

    if min_delay is not None or max_delay is not None:
        if not isinstance(entry, Iopath | Port | Interconnect | Device | NetDelay):
            return False
        scalar = entry.delay.scalar(corner=corner, edge=edge)
        if min_delay is not None and scalar < min_delay:
            return False
        if max_delay is not None and scalar > max_delay:
            return False

    return True
