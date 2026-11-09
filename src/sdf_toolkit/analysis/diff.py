"""Compare two SDF files and report differences.

Entries match by arc key within one cell instance, so the order of entries
in the files does not matter.  A file may repeat one arc; the n-th
occurrence in one file pairs with the n-th occurrence in the other, and an
occurrence without a partner counts as present in one file only.

Values compare as numbers only within one timescale, so files whose
timescales in force differ must be normalized first.
"""

from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from sdf_toolkit.core.checks import (
    BidirectSkew,
    Hold,
    NoChange,
    Period,
    Recovery,
    Recrem,
    Removal,
    Setup,
    SetupHold,
    Skew,
    Width,
)
from sdf_toolkit.core.delays import (
    ArcKey,
    Device,
    Interconnect,
    Iopath,
    NetDelay,
    PathPulse,
    PathPulsePercent,
    Port,
)
from sdf_toolkit.core.enums import Corner, HeaderField, TimeUnit
from sdf_toolkit.core.sdffile import Entry, Label, SDFFile, group_by_key
from sdf_toolkit.core.timingenv import (
    Arrival,
    Departure,
    DiffConstraint,
    PathConstraint,
    PeriodConstraint,
    SkewConstraint,
    Slack,
    SumConstraint,
    Waveform,
)
from sdf_toolkit.core.values import (
    DEFAULT_TOLERANCE,
    Number,
    Timescale,
    UnalignableDelayError,
    Values,
    to_decimal,
)
from sdf_toolkit.errors import SDFToolkitError
from sdf_toolkit.transform.normalize import normalize_delays

_ONE_PS = Timescale(value=1, unit=TimeUnit.PS)


class TimescaleMismatchError(SDFToolkitError, ValueError):
    """Two files in different timescales were compared without normalising."""


class ValueSlot(StrEnum):
    """The field of an entry that holds a value triple."""

    DELAY = "delay"
    LIMIT = "limit"
    SETUP = "setup"
    HOLD = "hold"
    RISE = "rise"
    FALL = "fall"
    REJECT = "reject"
    ERROR = "error"
    RECOVERY = "recovery"
    REMOVAL = "removal"
    FIRST_LIMIT = "first_limit"
    SECOND_LIMIT = "second_limit"
    EARLIEST_RISE = "earliest_rise"
    LATEST_RISE = "latest_rise"
    EARLIEST_FALL = "earliest_fall"
    LATEST_FALL = "latest_fall"
    RISE_SETUP = "rise_setup"
    FALL_SETUP = "fall_setup"
    RISE_HOLD = "rise_hold"
    FALL_HOLD = "fall_hold"
    PERIOD = "period"


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


@dataclass(frozen=True, slots=True)
class EntryLocation:
    """The cell instance and arc of an entry that only one file has."""

    cell_type: str
    instance: str
    key: ArcKey

    def __str__(self) -> str:
        """Write the location as ``cell_type/instance/arc``."""
        return f"{self.cell_type}/{self.instance}/{self.key}"


@dataclass(frozen=True, slots=True)
class _FieldDiff:
    """One differing value of an entry pair, before its location is attached."""

    field: DiffField
    value_a: Decimal | None
    value_b: Decimal | None
    delta: Decimal | None


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
    value_a : Decimal | None
        The value from the first SDF file.
    value_b : Decimal | None
        The value from the second SDF file.
    delta : Decimal | None
        ``value_b - value_a`` when both are present, else None.
    """

    cell_type: str
    instance: str
    key: ArcKey
    occurrence: int
    field: DiffField
    value_a: Decimal | None
    value_b: Decimal | None
    delta: Decimal | None


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
        Header fields that differ, as ``(value_in_a, value_in_b)``, read
        before any normalisation.
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
    tolerance: Decimal,
) -> list[_FieldDiff]:
    """Return one difference for each corner of the triple that differs."""
    diffs: list[_FieldDiff] = []
    for corner in Corner:
        val_a = values_a.at(corner)
        val_b = values_b.at(corner)
        if val_a is None and val_b is None:
            continue
        delta: Decimal | None = None
        if val_a is not None and val_b is not None:
            delta = val_b - val_a
            if abs(delta) <= tolerance:
                continue
        diffs.append(
            _FieldDiff(
                field=ValueField(slot=slot, corner=corner, rvalue=rvalue),
                value_a=val_a,
                value_b=val_b,
                delta=delta,
            )
        )
    return diffs


class UnsupportedDiffError(SDFToolkitError, ValueError):
    """An entry in both files holds a value diff cannot compare."""


def _slots(entry: Entry) -> list[tuple[ValueSlot, Values]]:
    """Return the value triples of an entry that is not a delay.

    Raises
    ------
    TypeError
        If *entry* is a delay, which has rvalues rather than fixed slots.
    UnsupportedDiffError
        If *entry* is a WAVEFORM.
    """
    match entry:
        case Iopath() | Port() | Interconnect() | Device() | NetDelay() | Label():
            msg = "a delay entry compares only with another delay entry"
            raise TypeError(msg)
        case PathPulse() | PathPulsePercent():
            # An omitted X limit compares as an empty triple.
            error = Values() if entry.error is None else entry.error
            return [(ValueSlot.REJECT, entry.reject), (ValueSlot.ERROR, error)]
        case Setup() | Hold() | Recovery() | Removal() | Skew() | Width() | Period():
            return [(ValueSlot.LIMIT, entry.limit)]
        case SetupHold():
            return [(ValueSlot.SETUP, entry.setup), (ValueSlot.HOLD, entry.hold)]
        case Recrem():
            return [
                (ValueSlot.RECOVERY, entry.recovery),
                (ValueSlot.REMOVAL, entry.removal),
            ]
        case BidirectSkew():
            return [
                (ValueSlot.FIRST_LIMIT, entry.first_limit),
                (ValueSlot.SECOND_LIMIT, entry.second_limit),
            ]
        case NoChange():
            return [(ValueSlot.SETUP, entry.setup), (ValueSlot.HOLD, entry.hold)]
        case PathConstraint():
            return [(ValueSlot.RISE, entry.rise), (ValueSlot.FALL, entry.fall)]
        case SumConstraint() | DiffConstraint():
            # An omitted fall limit compares as an empty triple.
            fall = Values() if entry.fall is None else entry.fall
            return [(ValueSlot.RISE, entry.rise), (ValueSlot.FALL, fall)]
        case PeriodConstraint() | SkewConstraint():
            return [(ValueSlot.LIMIT, entry.limit)]
        case Arrival() | Departure():
            return [
                (ValueSlot.EARLIEST_RISE, entry.earliest_rise),
                (ValueSlot.LATEST_RISE, entry.latest_rise),
                (ValueSlot.EARLIEST_FALL, entry.earliest_fall),
                (ValueSlot.LATEST_FALL, entry.latest_fall),
            ]
        case Slack():
            # The period is one number, so it compares the same at every corner.
            period = Values() if entry.period is None else Values.uniform(entry.period)
            return [
                (ValueSlot.RISE_SETUP, entry.rise_setup),
                (ValueSlot.FALL_SETUP, entry.fall_setup),
                (ValueSlot.RISE_HOLD, entry.rise_hold),
                (ValueSlot.FALL_HOLD, entry.fall_hold),
                (ValueSlot.PERIOD, period),
            ]
        case Waveform():
            msg = (
                f"{entry.key}: a WAVEFORM edge list is not a value triple, so diff "
                "cannot compare it. Remove the WAVEFORM from both files."
            )
            raise UnsupportedDiffError(msg)


def _compare_entries(
    entry_a: Entry,
    entry_b: Entry,
    tolerance: Decimal,
) -> list[_FieldDiff]:
    """Compare two entries with the same arc key, field by field.

    Delays with different rvalue counts compare transition by transition
    after alignment; a pair that cannot align reports an
    {class}`RvalueCountField`.
    """
    delays = Iopath | Port | Interconnect | Device | NetDelay | Label
    if isinstance(entry_a, delays) and isinstance(entry_b, delays):
        try:
            rvalues_a, rvalues_b = entry_a.delay.aligned(entry_b.delay)
        except UnalignableDelayError:
            count_a = len(entry_a.delay.rvalues)
            count_b = len(entry_b.delay.rvalues)
            return [
                _FieldDiff(
                    field=RvalueCountField(),
                    value_a=Decimal(count_a),
                    value_b=Decimal(count_b),
                    delta=Decimal(count_b - count_a),
                )
            ]
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
    tolerance: Decimal,
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
                    field=found.field,
                    value_a=found.value_a,
                    value_b=found.value_b,
                    delta=found.delta,
                )
                for found in _compare_entries(entry_a, entry_b, tolerance)
            )
        result.only_in_a.extend(
            EntryLocation(cell_type=cell_type, instance=instance, key=key)
            for _ in group_a[len(group_b) :]
        )

    for key, group_b in groups_b.items():
        surplus = len(group_b) - len(groups_a.get(key, []))
        result.only_in_b.extend(
            EntryLocation(cell_type=cell_type, instance=instance, key=key)
            for _ in range(surplus)
        )


def diff(
    a: SDFFile,
    b: SDFFile,
    *,
    tolerance: Number = DEFAULT_TOLERANCE,
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
    tolerance : Number, optional
        Absolute tolerance for value comparison, by default
        {data}`~sdf_toolkit.core.values.DEFAULT_TOLERANCE`.
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
    UnsupportedDiffError
        If an arc in both files is a WAVEFORM.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.diff import diff
    >>> from sdf_toolkit.core.values import DelayValues, Values
    >>> from sdf_toolkit.core.delays import PortSpec
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
    Decimal('2.0')
    """
    tolerance = to_decimal(tolerance)
    result = DiffResult()
    # Before normalising, so a normalised diff still shows each TIMESCALE.
    header_a = a.header.to_dict()
    header_b = b.header.to_dict()
    for hdr_field in HeaderField:
        val_a = header_a.get(hdr_field)
        val_b = header_b.get(hdr_field)
        if val_a != val_b:
            result.header_diffs[hdr_field] = (val_a, val_b)

    if normalize_first:
        a = normalize_delays(a, target_timescale)
        b = normalize_delays(b, target_timescale)
    elif a.header.effective_timescale != b.header.effective_timescale:
        msg = (
            f"Files are in different timescales, {a.header.effective_timescale} "
            f"and {b.header.effective_timescale}. Normalise first with "
            "normalize_first=True, or --normalize on the command line."
        )
        raise TimescaleMismatchError(msg)

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
