"""Merge two or more SDF files into one."""

import copy

from sdf_toolkit.core.model import BaseEntry, CaseInsensitiveStrEnum, SDFFile
from sdf_toolkit.transform.normalize import normalize_delays


class ConflictStrategy(CaseInsensitiveStrEnum):
    """Strategy for handling conflicting entries during merge."""

    KEEP_FIRST = "keep-first"
    KEEP_LAST = "keep-last"
    ERROR = "error"


def merge(
    files: list[SDFFile],
    strategy: ConflictStrategy = ConflictStrategy.KEEP_LAST,
    target_timescale: str | None = None,
) -> SDFFile:
    """Merge two or more SDF files into one.

    Parameters
    ----------
    files : list[SDFFile]
        The SDF files to merge.
    strategy : ConflictStrategy
        How to handle conflicting entries (same cell_type, instance,
        entry_name).
    target_timescale : str | None
        If set, normalize all files to this timescale before merging.

    Returns
    -------
    SDFFile
        The merged SDF file.

    Raises
    ------
    ValueError
        If files is empty, or if timescales differ and no
        target_timescale given, or if strategy is ERROR and conflicts
        exist.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.transform.merge import merge
    >>> sdf_a = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("BUF", "b0")
    ...         .add_iopath("A", "Y", {
    ...             "nominal": {"min": 1.0, "avg": 2.0, "max": 3.0},
    ...         })
    ...     .build()
    ... )
    >>> sdf_b = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("INV", "i0")
    ...         .add_iopath("A", "Y", {
    ...             "nominal": {"min": 4.0, "avg": 5.0, "max": 6.0},
    ...         })
    ...     .build()
    ... )
    >>> merged = merge([sdf_a, sdf_b])
    >>> sorted(merged.cells.keys())
    ['BUF', 'INV']
    """
    if not files:
        msg = "No files to merge"
        raise ValueError(msg)

    prepared = _prepare_files(files, target_timescale)

    header = copy.deepcopy(prepared[0].header)
    if target_timescale is not None:
        header.timescale = target_timescale

    result = SDFFile(header=header, cells={})

    for sdf in prepared:
        _merge_cells(result, sdf, strategy)

    return result


def _prepare_files(
    files: list[SDFFile],
    target_timescale: str | None,
) -> list[SDFFile]:
    """Normalize files to a common timescale, or validate they already match.

    Parameters
    ----------
    files : list[SDFFile]
        The SDF files to prepare.
    target_timescale : str | None
        If set, normalize each file to this timescale. Otherwise,
        validate that all files share the same timescale.

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

    timescales = {f.header.timescale for f in files}
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

    Parameters
    ----------
    result : SDFFile
        The accumulating merged SDF file (mutated in place).
    source : SDFFile
        The source SDF file whose cells are being merged.
    strategy : ConflictStrategy
        How to handle conflicting entries.

    Raises
    ------
    ValueError
        If strategy is ERROR and a conflicting entry is found.
    """
    for cell_type, instances in source.cells.items():
        for instance, entries in instances.items():
            for entry_name, entry in entries.items():
                _insert_entry(
                    result,
                    cell_type,
                    instance,
                    entry_name,
                    entry,
                    strategy,
                )


def _insert_entry(  # noqa: PLR0913
    result: SDFFile,
    cell_type: str,
    instance: str,
    entry_name: str,
    entry: BaseEntry,
    strategy: ConflictStrategy,
) -> None:
    """Insert a single entry into the result, applying the conflict strategy.

    Parameters
    ----------
    result : SDFFile
        The accumulating merged SDF file (mutated in place).
    cell_type : str
        The cell type key.
    instance : str
        The instance key.
    entry_name : str
        The entry name key.
    entry : BaseEntry
        The entry to insert.
    strategy : ConflictStrategy
        How to handle conflicting entries.

    Raises
    ------
    ValueError
        If strategy is ERROR and the entry already exists.
    """
    cell_dict = result.cells.setdefault(cell_type, {})
    inst_dict = cell_dict.setdefault(instance, {})

    if entry_name in inst_dict:
        if strategy == ConflictStrategy.KEEP_FIRST:
            return
        if strategy == ConflictStrategy.ERROR:
            msg = (
                f"Conflicting entry: cell_type={cell_type!r}, "
                f"instance={instance!r}, entry_name={entry_name!r}"
            )
            raise ValueError(msg)
        # KEEP_LAST: fall through to overwrite
    inst_dict[entry_name] = copy.deepcopy(entry)
