"""Compare two SDF files and report differences.

Entries match by arc key within one cell instance, so the order of entries
in the files does not matter.  A file may repeat one arc; the n-th
occurrence in one file pairs with the n-th occurrence in the other, and an
occurrence without a partner counts as present in one file only.
"""

from dataclasses import dataclass, field

from sdf_toolkit.core.model import (
    ArcKey,
    Corner,
    Device,
    Entry,
    HeaderField,
    Interconnect,
    Iopath,
    Port,
    SDFFile,
    Timescale,
    TimeUnit,
    Values,
    group_by_key,
    labelled_values,
)
from sdf_toolkit.transform.normalize import normalize_delays

# (cell type, instance, arc key) of one entry occurrence.
EntryLocation = tuple[str, str, ArcKey]

_ONE_PS = Timescale(value=1, unit=TimeUnit.PS)


@dataclass
class DiffEntry:
    """A single value difference between two SDF files.

    Attributes
    ----------
    cell_type : str
        The cell type name.
    instance : str
        The cell instance name.
    key : ArcKey
        The arc both entries time.
    occurrence : int
        Which occurrence of *key* in the instance, counting from 0.
    field : str
        The dotted field path, e.g. ``"delay[0].max"`` or ``"limit.min"``.
        ``"delay.count"`` reports delays whose rvalue counts cannot be
        aligned, with the counts as values.
    value_a : float | None
        The value from the first SDF file.
    value_b : float | None
        The value from the second SDF file.
    delta : float | None
        ``value_b - value_a`` when both are present, else None.
    """

    cell_type: str
    instance: str
    key: ArcKey
    occurrence: int
    field: str
    value_a: float | None
    value_b: float | None
    delta: float | None


@dataclass
class DiffResult:
    """Complete comparison result between two SDF files.

    Attributes
    ----------
    only_in_a : list[EntryLocation]
        Entries present only in the first file, in its file order.
    only_in_b : list[EntryLocation]
        Entries present only in the second file, in its file order.
    value_diffs : list[DiffEntry]
        Per-field value differences for entries present in both files.
    header_diffs : dict[HeaderField, tuple[str | None, str | None]]
        Header fields that differ, as ``(value_in_a, value_in_b)``.
    """

    only_in_a: list[EntryLocation] = field(default_factory=list)
    only_in_b: list[EntryLocation] = field(default_factory=list)
    value_diffs: list[DiffEntry] = field(default_factory=list)
    header_diffs: dict[HeaderField, tuple[str | None, str | None]] = field(
        default_factory=dict,
    )


def _compare_values(
    label: str,
    values_a: Values,
    values_b: Values,
    tolerance: float,
) -> list[tuple[str, float | None, float | None, float | None]]:
    """Return ``(field, a, b, delta)`` for each corner that differs."""
    diffs: list[tuple[str, float | None, float | None, float | None]] = []
    for corner in Corner:
        val_a = values_a.at(corner)
        val_b = values_b.at(corner)
        if val_a is None and val_b is None:
            continue
        delta: float | None = None
        if val_a is not None and val_b is not None:
            delta = val_b - val_a
            if abs(delta) <= tolerance:
                continue
        diffs.append((f"{label}.{corner}", val_a, val_b, delta))
    return diffs


def _compare_entries(
    entry_a: Entry,
    entry_b: Entry,
    tolerance: float,
) -> list[tuple[str, float | None, float | None, float | None]]:
    """Compare two entries with the same arc key, field by field.

    Delays with different rvalue counts compare transition by transition
    after alignment; a pair that cannot align reports ``delay.count``.
    """
    if isinstance(entry_a, Iopath | Port | Interconnect | Device) and isinstance(
        entry_b, Iopath | Port | Interconnect | Device
    ):
        try:
            rvalues_a, rvalues_b = entry_a.delay.aligned(entry_b.delay)
        except ValueError:
            count_a = len(entry_a.delay.rvalues)
            count_b = len(entry_b.delay.rvalues)
            return [("delay.count", count_a, count_b, count_b - count_a)]
        pairs = [
            (f"delay[{i}]", a, b)
            for i, (a, b) in enumerate(zip(rvalues_a, rvalues_b, strict=True))
        ]
    else:
        pairs = [
            (label, a, b)
            for (label, a), (_, b) in zip(
                labelled_values(entry_a), labelled_values(entry_b), strict=True
            )
        ]
    return [
        diff
        for label, a, b in pairs
        for diff in _compare_values(label, a, b, tolerance)
    ]


def _compare_instance(
    cell_type: str,
    instance: str,
    entries_a: list[Entry],
    entries_b: list[Entry],
    result: DiffResult,
    tolerance: float,
) -> None:
    """Match the entries of one instance by arc key and record differences."""
    groups_a = group_by_key(entries_a)
    groups_b = group_by_key(entries_b)

    for key, group_a in groups_a.items():
        group_b = groups_b.get(key, [])
        # Surplus occurrences on either side are reported as only-in below.
        for occurrence, (entry_a, entry_b) in enumerate(
            zip(group_a, group_b, strict=False)
        ):
            result.value_diffs.extend(
                DiffEntry(
                    cell_type=cell_type,
                    instance=instance,
                    key=key,
                    occurrence=occurrence,
                    field=field_name,
                    value_a=val_a,
                    value_b=val_b,
                    delta=delta,
                )
                for field_name, val_a, val_b, delta in _compare_entries(
                    entry_a, entry_b, tolerance
                )
            )
        result.only_in_a.extend(
            (cell_type, instance, key) for _ in group_a[len(group_b) :]
        )

    for key, group_b in groups_b.items():
        surplus = len(group_b) - len(groups_a.get(key, []))
        result.only_in_b.extend((cell_type, instance, key) for _ in range(surplus))


def diff(
    a: SDFFile,
    b: SDFFile,
    *,
    tolerance: float = 1e-9,
    normalize_first: bool = False,
    target_timescale: Timescale = _ONE_PS,
) -> DiffResult:
    """Compare two SDF files and return a structured diff result.

    Parameters
    ----------
    a : SDFFile
        The first (reference) SDF file.
    b : SDFFile
        The second (comparison) SDF file.
    tolerance : float, optional
        Absolute tolerance for floating-point value comparison, by default
        1e-9.
    normalize_first : bool, optional
        If True, normalize both files to ``target_timescale`` before
        comparing, by default False.
    target_timescale : Timescale, optional
        The timescale to normalize to when ``normalize_first`` is True,
        by default 1 ps.

    Returns
    -------
    DiffResult
        The complete comparison result including header diffs, entries
        only in one file, and per-field value differences.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.diff import diff
    >>> from sdf_toolkit.core.model import DelayValues, PortSpec, Values
    >>> def buf(max_delay):
    ...     return dict(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=max_delay)),
    ...     )
    >>> a = SDFBuilder().add_cell("BUF", "b0").add_iopath(**buf(3.0)).build()
    >>> b = (
    ...     SDFBuilder()
    ...     .add_cell("BUF", "b0")
    ...     .add_iopath(**buf(5.0))
    ...     .add_cell("INV", "i0")
    ...     .add_iopath(**buf(3.0))
    ...     .build()
    ... )
    >>> result = diff(a, b)
    >>> len(result.only_in_b)
    1
    >>> result.value_diffs[0].field
    'delay[0].max'
    >>> result.value_diffs[0].delta
    2.0
    """
    if normalize_first:
        a = normalize_delays(a, target_timescale)
        b = normalize_delays(b, target_timescale)

    result = DiffResult()

    header_a = a.header.to_dict()
    header_b = b.header.to_dict()
    for hdr_field in HeaderField:
        val_a = header_a.get(hdr_field)
        val_b = header_b.get(hdr_field)
        if val_a != val_b:
            result.header_diffs[hdr_field] = (val_a, val_b)

    for cell_type, instances_a in a.cells.items():
        instances_b = b.cells.get(cell_type, {})
        for instance, entries_a in instances_a.items():
            _compare_instance(
                cell_type,
                instance,
                entries_a,
                instances_b.get(instance, []),
                result,
                tolerance,
            )
    for cell_type, instances_b in b.cells.items():
        instances_a = a.cells.get(cell_type, {})
        for instance, entries_b in instances_b.items():
            if instance not in instances_a:
                _compare_instance(cell_type, instance, [], entries_b, result, tolerance)

    return result
