"""Compare two SDF files and report differences.

Entries match by arc key within one cell instance, so the order of entries
in the files does not matter.  A file may repeat one arc; the n-th
occurrence in one file pairs with the n-th occurrence in the other, and an
occurrence without a partner counts as present in one file only.

Values compare as numbers only within one timescale, so files whose
timescales in force differ must be normalized first.
"""

from dataclasses import dataclass, field
from enum import StrEnum

from sdf_toolkit.core.model import (
    ArcKey,
    Corner,
    Device,
    Entry,
    HeaderField,
    Hold,
    Interconnect,
    Iopath,
    PathConstraint,
    Period,
    Port,
    Recovery,
    Removal,
    SDFFile,
    Setup,
    SetupHold,
    Timescale,
    TimeUnit,
    Values,
    Width,
    group_by_key,
)
from sdf_toolkit.transform.normalize import normalize_delays

# (cell type, instance, arc key) of one entry occurrence.
EntryLocation = tuple[str, str, ArcKey]

_ONE_PS = Timescale(value=1, unit=TimeUnit.PS)


class ValueSlot(StrEnum):
    """The field of an entry that holds a value triple."""

    DELAY = "delay"
    LIMIT = "limit"
    SETUP = "setup"
    HOLD = "hold"
    RISE = "rise"
    FALL = "fall"


@dataclass(frozen=True, slots=True)
class ValueField:
    """One corner of one value triple; *rvalue* indexes a delay in file order."""

    slot: ValueSlot
    corner: Corner
    rvalue: int | None = None

    def __str__(self) -> str:
        """Write the field as ``delay[0].max`` or ``limit.min``."""
        index = "" if self.rvalue is None else f"[{self.rvalue}]"
        return f"{self.slot}{index}.{self.corner}"


@dataclass(frozen=True, slots=True)
class RvalueCountField:
    """Two delays whose rvalue counts cannot be aligned; the values are the counts."""

    def __str__(self) -> str:
        """Write the field as ``delay.count``."""
        return "delay.count"


DiffField = ValueField | RvalueCountField
# (field, value in a, value in b, b - a when both are present)
_FieldDiff = tuple[DiffField, float | None, float | None, float | None]


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
    field : DiffField
        The differing value; its text form reads ``delay[0].max`` or
        ``limit.min``.  An {class}`RvalueCountField` reports delays whose
        rvalue counts cannot be aligned, with the counts as values.
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
    field: DiffField
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
    *,
    slot: ValueSlot,
    rvalue: int | None,
    values_a: Values,
    values_b: Values,
    tolerance: float,
) -> list[_FieldDiff]:
    """Return one difference for each corner of the triple that differs."""
    diffs: list[_FieldDiff] = []
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
        diffs.append(
            (ValueField(slot=slot, corner=corner, rvalue=rvalue), val_a, val_b, delta)
        )
    return diffs


def _slots(entry: Entry) -> list[tuple[ValueSlot, Values]]:
    """Return the value triples of a timing check or path constraint.

    Raises
    ------
    TypeError
        If *entry* is a delay, which has rvalues rather than fixed slots.
    """
    match entry:
        case Iopath() | Port() | Interconnect() | Device():
            msg = "a delay entry compares only with another delay entry"
            raise TypeError(msg)
        case Setup() | Hold() | Recovery() | Removal() | Width() | Period():
            return [(ValueSlot.LIMIT, entry.limit)]
        case SetupHold():
            return [(ValueSlot.SETUP, entry.setup), (ValueSlot.HOLD, entry.hold)]
        case PathConstraint():
            return [(ValueSlot.RISE, entry.rise), (ValueSlot.FALL, entry.fall)]


def _compare_entries(
    entry_a: Entry,
    entry_b: Entry,
    tolerance: float,
) -> list[_FieldDiff]:
    """Compare two entries with the same arc key, field by field.

    Delays with different rvalue counts compare transition by transition
    after alignment; a pair that cannot align reports an
    {class}`RvalueCountField`.
    """
    if isinstance(entry_a, Iopath | Port | Interconnect | Device) and isinstance(
        entry_b, Iopath | Port | Interconnect | Device
    ):
        try:
            rvalues_a, rvalues_b = entry_a.delay.aligned(entry_b.delay)
        except ValueError:
            count_a = len(entry_a.delay.rvalues)
            count_b = len(entry_b.delay.rvalues)
            return [(RvalueCountField(), count_a, count_b, count_b - count_a)]
        pairs = [
            (ValueSlot.DELAY, i, a, b)
            for i, (a, b) in enumerate(zip(rvalues_a, rvalues_b, strict=True))
        ]
    else:
        pairs = [
            (slot, None, a, b)
            for (slot, a), (_, b) in zip(_slots(entry_a), _slots(entry_b), strict=True)
        ]
    return [
        diff
        for slot, rvalue, a, b in pairs
        for diff in _compare_values(
            slot=slot, rvalue=rvalue, values_a=a, values_b=b, tolerance=tolerance
        )
    ]


def _compare_instance(
    *,
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
                    field=diff_field,
                    value_a=val_a,
                    value_b=val_b,
                    delta=delta,
                )
                for diff_field, val_a, val_b, delta in _compare_entries(
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
        comparing, by default False.  Without it the files must share the
        timescale in force.
    target_timescale : Timescale, optional
        The timescale to normalize to when ``normalize_first`` is True,
        by default 1 ps.

    Returns
    -------
    DiffResult
        The complete comparison result including header diffs, entries
        only in one file, and per-field value differences.

    Raises
    ------
    ValueError
        If *normalize_first* is False and the timescales in force differ.

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
    >>> str(result.value_diffs[0].field)
    'delay[0].max'
    >>> result.value_diffs[0].delta
    2.0
    """
    if normalize_first:
        a = normalize_delays(a, target_timescale)
        b = normalize_delays(b, target_timescale)
    elif a.header.effective_timescale != b.header.effective_timescale:
        msg = (
            f"Files are in different timescales, {a.header.effective_timescale} "
            f"and {b.header.effective_timescale}; pass normalize_first=True"
        )
        raise ValueError(msg)

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
                cell_type=cell_type,
                instance=instance,
                entries_a=entries_a,
                entries_b=instances_b.get(instance, []),
                result=result,
                tolerance=tolerance,
            )
    for cell_type, instances_b in b.cells.items():
        instances_a = a.cells.get(cell_type, {})
        for instance, entries_b in instances_b.items():
            if instance not in instances_a:
                _compare_instance(
                    cell_type=cell_type,
                    instance=instance,
                    entries_a=[],
                    entries_b=entries_b,
                    result=result,
                    tolerance=tolerance,
                )

    return result
