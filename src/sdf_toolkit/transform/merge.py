"""Merge two or more SDF files into one.

Entries conflict when they time the same arc in the same cell instance.  A
file may repeat one arc, so the n-th occurrence of an arc in a later file
conflicts with the n-th occurrence already merged, and a further
occurrence is added.
"""

from sdf_toolkit.core.model import (
    ArcKey,
    CaseInsensitiveStrEnum,
    Entry,
    SDFFile,
    Timescale,
)
from sdf_toolkit.transform.normalize import normalize_delays


class ConflictStrategy(CaseInsensitiveStrEnum):
    """Strategy for handling conflicting entries during merge."""

    KEEP_FIRST = "keep-first"
    KEEP_LAST = "keep-last"
    ERROR = "error"


def merge(
    files: list[SDFFile],
    *,
    strategy: ConflictStrategy = ConflictStrategy.KEEP_LAST,
    target_timescale: Timescale | None = None,
) -> SDFFile:
    """Merge two or more SDF files into one.

    Parameters
    ----------
    files : list[SDFFile]
        The SDF files to merge.
    strategy : ConflictStrategy
        How to handle entries that time the same arc of one instance.
        ``KEEP_LAST`` replaces the earlier entry in its place.
    target_timescale : Timescale | None
        If set, normalize all files to this timescale before merging.

    Returns
    -------
    SDFFile
        The merged SDF file.

    Raises
    ------
    ValueError
        If files is empty, or if the timescales in force differ and no
        target_timescale is given, or if strategy is ERROR and conflicts
        exist.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.transform.merge import merge
    >>> from sdf_toolkit.core.model import DelayValues, PortSpec, Values
    >>> buf = dict(
    ...     input=PortSpec("A"),
    ...     output=PortSpec("Y"),
    ...     delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ... )
    >>> sdf_a = SDFBuilder().add_cell("BUF", "b0").add_iopath(**buf).build()
    >>> sdf_b = SDFBuilder().add_cell("INV", "i0").add_iopath(**buf).build()
    >>> merged = merge([sdf_a, sdf_b])
    >>> sorted(merged.cells.keys())
    ['BUF', 'INV']
    """
    if not files:
        msg = "No files to merge"
        raise ValueError(msg)

    prepared = _prepare_files(files, target_timescale)

    # normalize_delays already set the target timescale in each header.
    result = SDFFile(header=prepared[0].header, cells={})

    for sdf in prepared:
        _merge_cells(result, sdf, strategy)

    return result


def _prepare_files(
    files: list[SDFFile],
    target_timescale: Timescale | None,
) -> list[SDFFile]:
    """Normalize files to a common timescale, or validate they already match.

    Parameters
    ----------
    files : list[SDFFile]
        The SDF files to prepare.
    target_timescale : Timescale | None
        If set, normalize each file to this timescale. Otherwise,
        validate that all files share the timescale in force.

    Returns
    -------
    list[SDFFile]
        The prepared (possibly normalized) SDF files.

    Raises
    ------
    ValueError
        If no target_timescale is given and files have differing
        timescales.
    """
    if target_timescale is not None:
        return [normalize_delays(f, target_timescale) for f in files]

    timescales = {f.header.effective_timescale for f in files}
    if len(timescales) > 1:
        msg = (
            f"Files have differing timescales {timescales} "
            f"and no target_timescale was specified"
        )
        raise ValueError(msg)

    return files


def _merge_cells(
    result: SDFFile,
    source: SDFFile,
    strategy: ConflictStrategy,
) -> None:
    """Merge cells from *source* into *result* in place.

    Raises
    ------
    ValueError
        If strategy is ERROR and a conflicting entry is found.
    """
    for cell_type, instances in source.cells.items():
        for instance, entries in instances.items():
            merged = result.cells.setdefault(cell_type, {}).setdefault(instance, [])
            positions: dict[ArcKey, list[int]] = {}
            for index, entry in enumerate(merged):
                positions.setdefault(entry.key, []).append(index)
            seen: dict[ArcKey, int] = {}
            for entry in entries:
                occurrence = seen.get(entry.key, 0)
                seen[entry.key] = occurrence + 1
                slots = positions.setdefault(entry.key, [])
                if occurrence == len(slots):
                    slots.append(len(merged))
                    merged.append(entry)
                    continue
                _resolve_conflict(
                    merged,
                    index=slots[occurrence],
                    entry=entry,
                    strategy=strategy,
                    location=f"cell_type={cell_type!r}, instance={instance!r}",
                )


def _resolve_conflict(
    merged: list[Entry],
    *,
    index: int,
    entry: Entry,
    strategy: ConflictStrategy,
    location: str,
) -> None:
    """Apply *strategy* to *entry*, which conflicts with ``merged[index]``.

    Raises
    ------
    ValueError
        If strategy is ERROR.
    """
    match strategy:
        case ConflictStrategy.KEEP_FIRST:
            return
        case ConflictStrategy.KEEP_LAST:
            merged[index] = entry
        case ConflictStrategy.ERROR:
            msg = f"Conflicting entry: {location}, arc={entry.key}"
            raise ValueError(msg)
