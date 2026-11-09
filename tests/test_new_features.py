from dataclasses import replace
from decimal import Decimal

import pytest
from conftest import DATA_DIR

from sdf_toolkit.analysis.diff import (
    EntryLocation,
    RvalueCountField,
    UnsupportedDiffError,
    ValueField,
    ValueSlot,
    diff,
)
from sdf_toolkit.analysis.paths import batch_endpoint_analysis
from sdf_toolkit.analysis.query import query
from sdf_toolkit.analysis.report import _format_delay, generate_report
from sdf_toolkit.analysis.stats import compute_stats
from sdf_toolkit.analysis.validate import IssueSeverity, validate
from sdf_toolkit.core.checks import NoChange, Recrem, Setup
from sdf_toolkit.core.delays import (
    Cond,
    Device,
    Interconnect,
    Iopath,
    PathPulse,
    PathPulsePercent,
    Port,
    PortSpec,
    Retain,
    TimingPortSpec,
)
from sdf_toolkit.core.enums import (
    Corner,
    DelayMode,
    Divider,
    EdgeSelect,
    EdgeType,
    EntryType,
    HeaderField,
)
from sdf_toolkit.core.sdffile import CellsDict, Entry, Label, SDFFile, SDFHeader
from sdf_toolkit.core.timingenv import Arrival, Slack, Waveform, WaveformEdge
from sdf_toolkit.core.values import (
    DelayValues,
    MissingDelayError,
    PulseLimits,
    Timescale,
    Values,
)
from sdf_toolkit.parser.parser import parse_sdf
from sdf_toolkit.transform.merge import ConflictStrategy, MergeConflictError, merge
from sdf_toolkit.transform.normalize import normalize_delays


def _count_entries(cells: CellsDict) -> int:
    return sum(
        len(entries) for instances in cells.values() for entries in instances.values()
    )


def _parse(name: str) -> SDFFile:
    return parse_sdf((DATA_DIR / name).read_text())


def _first_delay(sdf: SDFFile) -> DelayValues:
    entry = next(iter(next(iter(sdf.cells.values())).values()))[0]
    assert isinstance(entry, Iopath | Port | Interconnect | Device)
    return entry.delay


_ONE = Values.uniform(1.0)


def _one_cell(*entries: Entry) -> SDFFile:
    return SDFFile(
        header=SDFHeader(timescale=Timescale.parse("1ps")),
        cells={"DFF": {"ff0": list(entries)}},
    )


def _iopath(*, edge: EdgeType | None, delay: float) -> Iopath:
    return Iopath(
        input=PortSpec("CLK", edge=edge),
        output=PortSpec("Q"),
        mode=DelayMode.ABSOLUTE,
        delay=DelayValues.of(Values.uniform(delay)),
    )


def _setup(*, data: TimingPortSpec, limit: float) -> Setup:
    return Setup(
        data=data,
        reference=TimingPortSpec("CLK", edge=EdgeType.POSEDGE),
        limit=Values.uniform(limit),
    )


# Pairs of entries that time different arcs over the same pins.  The old
# synthetic names keyed both members of each pair alike.
_DISTINCT_ARCS: list[tuple[str, tuple[Entry, Entry], tuple[Entry, Entry]]] = [
    (
        "iopath-posedge-negedge",
        (
            _iopath(edge=EdgeType.POSEDGE, delay=1.0),
            _iopath(edge=EdgeType.NEGEDGE, delay=2.0),
        ),
        (
            _iopath(edge=EdgeType.POSEDGE, delay=1.0),
            _iopath(edge=EdgeType.NEGEDGE, delay=3.0),
        ),
    ),
    (
        "setup-data-posedge-negedge",
        (
            _setup(data=TimingPortSpec("D", edge=EdgeType.POSEDGE), limit=1.0),
            _setup(data=TimingPortSpec("D", edge=EdgeType.NEGEDGE), limit=2.0),
        ),
        (
            _setup(data=TimingPortSpec("D", edge=EdgeType.POSEDGE), limit=1.0),
            _setup(data=TimingPortSpec("D", edge=EdgeType.NEGEDGE), limit=3.0),
        ),
    ),
    (
        "setup-data-cond",
        (
            _setup(data=TimingPortSpec("D"), limit=1.0),
            _setup(data=TimingPortSpec("D", cond=Cond("EN")), limit=2.0),
        ),
        (
            _setup(data=TimingPortSpec("D"), limit=1.0),
            _setup(data=TimingPortSpec("D", cond=Cond("EN")), limit=3.0),
        ),
    ),
]


def _ps(nanoseconds: float) -> Values:
    return Values.uniform(nanoseconds * 1000)


class TestNormalize:
    def test_normalize_ps_to_ns(self):
        sdf = _parse("test1.sdf")
        result = normalize_delays(sdf, Timescale.parse("1ns"))
        assert result.header.timescale == Timescale.parse("1ns")
        # 286 ps is 0.286 ns.
        assert _first_delay(result).rvalues[0].min == Decimal("0.286")

    def test_normalize_ns_to_ps(self):
        sdf = _parse("spec-example1.sdf")
        result = normalize_delays(sdf, Timescale.parse("1ps"))
        assert result.header.timescale == Timescale.parse("1ps")
        assert sdf.header.timescale == Timescale.parse("1ns")

    def test_missing_timescale_uses_1ns_default(self):
        sdf = replace(_one_cell(_iopath(edge=None, delay=2.0)), header=SDFHeader())
        result = normalize_delays(sdf, Timescale.parse("1ps"))
        assert _first_delay(result).rvalues[0].max == pytest.approx(2000.0)

    def test_normalize_same_timescale(self):
        sdf = _parse("test1.sdf")
        result = normalize_delays(sdf, Timescale.parse("1ps"))
        assert _first_delay(result) == _first_delay(sdf)

    def test_original_unmodified(self):
        sdf = _parse("test1.sdf")
        original_ts = sdf.header.timescale
        normalize_delays(sdf, Timescale.parse("1ns"))
        assert sdf.header.timescale == original_ts

    @pytest.mark.parametrize(
        ("value", "source", "target", "expected"),
        [
            # A precomputed 1e-4 factor gives 0.012345600000000002 here.
            (123.456, "1ps", "10ns", 0.0123456),
            (3.3, "10ps", "1ns", 0.033),
            (0.7, "1ns", "1ps", 700.0),
        ],
    )
    def test_rescale_uses_exact_ratio(self, value, source, target, expected):
        setup = _setup(data=TimingPortSpec("D"), limit=value)
        sdf = replace(
            _one_cell(_iopath(edge=None, delay=value), setup),
            header=SDFHeader(timescale=Timescale.parse(source)),
        )
        result = normalize_delays(sdf, Timescale.parse(target))
        iopath, check = result.cells["DFF"]["ff0"]
        assert isinstance(iopath, Iopath)
        assert isinstance(check, Setup)
        assert iopath.delay == DelayValues.of(Values.uniform(expected))
        assert check.limit == Values.uniform(expected)

    @pytest.mark.parametrize(
        ("body", "expected"),
        [
            pytest.param(
                "(DELAY (PATHPULSE a y (1) (2)))",
                PathPulse(path=("a", "y"), reject=_ps(1), error=_ps(2)),
                id="pathpulse",
            ),
            pytest.param(
                "(DELAY (PATHPULSEPERCENT (25)))",
                PathPulsePercent(path=None, reject=Values.uniform(25)),
                id="pathpulsepercent-is-not-a-time",
            ),
            pytest.param(
                "(DELAY (ABSOLUTE (IOPATH a y (RETAIN (1)) ((2) (3)))))",
                Iopath(
                    input=PortSpec("a"),
                    output=PortSpec("y"),
                    retain=(Retain(delay=DelayValues.of(_ps(1))),),
                    mode=DelayMode.ABSOLUTE,
                    delay=DelayValues(
                        rvalues=(_ps(2),), limits=(PulseLimits(reject=_ps(3)),)
                    ),
                ),
                id="retain-and-pulse-limits",
            ),
            pytest.param(
                "(TIMINGCHECK (RECREM r (posedge c) (1) (2) (SCOND en)))",
                Recrem(
                    data=TimingPortSpec(port="r"),
                    reference=TimingPortSpec(port="c", edge=EdgeType.POSEDGE),
                    recovery=_ps(1),
                    removal=_ps(2),
                    scond=Cond(equation="en"),
                ),
                id="recrem",
            ),
            pytest.param(
                "(TIMINGCHECK (NOCHANGE (posedge c) d (1) (2)))",
                NoChange(
                    control=TimingPortSpec(port="c", edge=EdgeType.POSEDGE),
                    data=TimingPortSpec(port="d"),
                    setup=_ps(1),
                    hold=_ps(2),
                ),
                id="nochange",
            ),
            pytest.param(
                "(TIMINGENV (ARRIVAL (posedge c) d (1) (2) (3) (4)))",
                Arrival(
                    reference=PortSpec(port="c", edge=EdgeType.POSEDGE),
                    port="d",
                    earliest_rise=_ps(1),
                    latest_rise=_ps(2),
                    earliest_fall=_ps(3),
                    latest_fall=_ps(4),
                ),
                id="arrival",
            ),
            pytest.param(
                "(TIMINGENV (SLACK d (1) (2) (3) (4) 5))",
                Slack(
                    port="d",
                    rise_setup=_ps(1),
                    fall_setup=_ps(2),
                    rise_hold=_ps(3),
                    fall_hold=_ps(4),
                    period=Decimal(5000),
                ),
                id="slack-period",
            ),
            pytest.param(
                "(TIMINGENV (WAVEFORM c 10 (posedge 0 1) (negedge 5)))",
                Waveform(
                    port="c",
                    period=Decimal(10000),
                    edges=(
                        WaveformEdge(
                            edge=EdgeType.POSEDGE, offsets=(Decimal(0), Decimal(1000))
                        ),
                        WaveformEdge(edge=EdgeType.NEGEDGE, offsets=(Decimal(5000),)),
                    ),
                ),
                id="waveform",
            ),
            pytest.param(
                "(LABEL (INCREMENT (tpd (1))))",
                Label(
                    name="tpd", mode=DelayMode.INCREMENT, delay=DelayValues.of(_ps(1))
                ),
                id="label",
            ),
        ],
    )
    def test_rescales_every_time_value(self, body: str, expected: Entry) -> None:
        sdf = parse_sdf(
            f'(DELAYFILE (TIMESCALE 1ns) (CELL (CELLTYPE "C") (INSTANCE u) {body}))'
        )
        result = normalize_delays(sdf, Timescale.parse("1ps"))
        assert result.cells["C"]["u"] == [expected]


class TestValidate:
    def test_valid_file_no_issues(self):
        assert validate(_parse("spec-example1.sdf")) == []

    def test_missing_timescale_warning(self):
        sdf = SDFFile(header=SDFHeader(), cells={"A": {"a0": []}})
        issues = validate(sdf)
        assert any("timescale" in i.message.lower() for i in issues)

    def test_empty_cells_warning(self):
        sdf = SDFFile(header=SDFHeader(timescale=Timescale.parse("1ps")), cells={})
        issues = validate(sdf)
        assert any("no cells" in i.message.lower() for i in issues)

    @pytest.mark.parametrize(
        "entry",
        [
            Iopath(
                input=PortSpec("A"),
                output=PortSpec("Y"),
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(Values.uniform(1.0), Values()),
            ),
            replace(_setup(data=TimingPortSpec("D"), limit=1.0), limit=Values()),
        ],
        ids=["delay-rvalue", "check-limit"],
    )
    def test_empty_triple_is_legal(self, entry):
        assert validate(_one_cell(entry)) == []

    @pytest.mark.parametrize(
        ("instance", "warns"),
        [("shared_inst", True), ("*", False), ("", True)],
        ids=["named", "wildcard", "top-level"],
    )
    def test_cross_cell_type_instance_warning(self, instance, warns):
        entry = _iopath(edge=None, delay=1.0)
        sdf = SDFFile(
            header=SDFHeader(timescale=Timescale.parse("1ps")),
            cells={"BUF": {instance: [entry]}, "INV": {instance: [entry]}},
        )
        issues = validate(sdf)
        assert [i.instance for i in issues] == ([instance] if warns else [])
        assert all(i.severity == IssueSeverity.WARNING for i in issues)
        assert all(i.cell_type is None for i in issues)

    @pytest.mark.parametrize(
        ("triple", "ordered"),
        [
            (Values(min=1.0, typ=2.0, max=3.0), True),
            (Values(min=-1.0, typ=0.0, max=1.0), True),
            (Values(min=2.0, typ=2.0, max=2.0), True),
            (Values(min=None, typ=2.0, max=None), True),
            (Values(min=3.0, typ=2.0, max=1.0), False),
            (Values(min=1.0, typ=None, max=0.5), False),
            (Values(min=None, typ=2.0, max=1.0), False),
        ],
    )
    @pytest.mark.parametrize(
        "make",
        [
            lambda v: replace(
                _iopath(edge=None, delay=1.0),
                delay=DelayValues.of(Values.uniform(0), v),
            ),
            lambda v: replace(_setup(data=TimingPortSpec("D"), limit=1.0), limit=v),
        ],
        ids=["delay", "limit"],
    )
    def test_corner_order_is_an_error(self, triple, ordered, make):
        issues = validate(_one_cell(make(triple)))
        if ordered:
            assert issues == []
        else:
            [issue] = issues
            assert issue.severity == IssueSeverity.ERROR
            assert (issue.cell_type, issue.instance) == ("DFF", "ff0")
            assert "min <= typ <= max" in issue.message

    def test_errors_sort_before_warnings(self):
        sdf = replace(
            _one_cell(
                _setup(data=TimingPortSpec("D"), limit=1.0),
                replace(
                    _setup(data=TimingPortSpec("E"), limit=1.0),
                    limit=Values(min=2.0, max=1.0),
                ),
            ),
            header=SDFHeader(),
        )
        assert [i.severity for i in validate(sdf)] == [
            IssueSeverity.ERROR,
            IssueSeverity.WARNING,
        ]

    def test_file_level_issue_has_no_location(self):
        [issue] = validate(SDFFile(header=SDFHeader(), cells={"A": {"a0": []}}))
        assert (issue.cell_type, issue.instance, issue.entry_name) == (None, None, None)


class TestStats:
    def test_stats_from_file(self):
        stats = compute_stats(_parse("test1.sdf"), corner=Corner.MIN)
        assert stats.total_cells > 0
        assert stats.total_instances > 0
        assert stats.total_entries > 0
        assert stats.delay_min is not None
        assert stats.delay_max is not None
        assert stats.delay_min <= stats.delay_max

    def test_stats_empty_sdf(self):
        stats = compute_stats(SDFFile())
        assert stats.total_cells == 0
        assert stats.total_entries == 0
        assert stats.delay_min is None
        assert stats.delay_mean is None

    def test_stats_entry_type_counts(self):
        stats = compute_stats(_parse("spec-example1.sdf"), corner=Corner.MIN)
        assert set(stats.entry_type_counts) == {
            EntryType.IOPATH,
            EntryType.INTERCONNECT,
        }
        assert stats.entry_type_counts.total() == stats.total_entries

    @pytest.mark.parametrize(
        ("rvalue", "corner", "missing", "delay_max"),
        [
            ("(2)", Corner.MAX, 0, 2.0),
            ("(2)", Corner.MIN, 0, 2.0),
            ("(:2:)", Corner.TYP, 0, 2.0),
            ("(:2:)", Corner.MAX, 2, None),
            ("()", Corner.MAX, 2, None),
        ],
    )
    def test_stats_on_single_rvalue_file(self, rvalue, corner, missing, delay_max):
        sdf = parse_sdf(
            '(DELAYFILE (SDFVERSION "3.0") (TIMESCALE 1ns)'
            ' (CELL (CELLTYPE "INV") (INSTANCE i0)'
            f" (DELAY (ABSOLUTE (IOPATH A Y {rvalue}) (IOPATH B Y {rvalue})))))"
        )
        stats = compute_stats(sdf, corner=corner)
        assert stats.entry_type_counts == {EntryType.IOPATH: 2}
        assert stats.missing_delay_count == missing
        assert stats.delay_max == delay_max


def _escaped_sdf() -> SDFFile:
    """One INV instance ``u[0]`` with a PORT on ``d[1]`` and an INTERCONNECT."""
    return parse_sdf(
        '(DELAYFILE (SDFVERSION "3.0") (DIVIDER /) (TIMESCALE 1ns)'
        r' (CELL (CELLTYPE "INV") (INSTANCE u\[0\])'
        r" (DELAY (ABSOLUTE (PORT d\[1\] (1)) (INTERCONNECT u\[0\]/Y b/A (2))))))"
    )


class TestQuery:
    def test_filter_by_cell_type(self):
        result = query(_parse("spec-example1.sdf"), cell_types=["INV"])
        assert "INV" in result.cells
        assert "OR2" not in result.cells

    def test_filter_by_entry_type(self):
        result = query(_parse("spec-example1.sdf"), entry_types=[EntryType.IOPATH])
        for instances in result.cells.values():
            for entries in instances.values():
                for entry in entries:
                    assert entry.type == EntryType.IOPATH

    def test_filter_by_pin_pattern(self):
        result = query(_parse("spec-example1.sdf"), pin_pattern="^z$")
        assert _count_entries(result.cells) > 0
        for instances in result.cells.values():
            for entries in instances.values():
                for entry in entries:
                    assert "z" in {pin.port for pin in entry.key.pins}

    def test_no_filters_returns_all(self):
        sdf = _parse("spec-example1.sdf")
        assert _count_entries(query(sdf).cells) == _count_entries(sdf.cells)

    def test_filter_preserves_header(self):
        sdf = _parse("spec-example1.sdf")
        assert query(sdf, cell_types=["INV"]).header == sdf.header

    @pytest.mark.parametrize(
        ("bounds", "check"),
        [
            ({"min_delay": 0.3}, lambda scalar: scalar >= 0.3),
            ({"max_delay": 0.1}, lambda scalar: scalar <= 0.1),
        ],
        ids=["min_delay", "max_delay"],
    )
    def test_delay_threshold(self, bounds, check):
        sdf = _parse("spec-example1.sdf")
        result = query(sdf, corner=Corner.MIN, edge=EdgeSelect.WORST, **bounds)
        kept = [
            entry
            for instances in result.cells.values()
            for entries in instances.values()
            for entry in entries
        ]
        assert 0 < len(kept) < _count_entries(sdf.cells)
        for entry in kept:
            assert isinstance(entry, Iopath | Port | Interconnect | Device)
            assert check(entry.delay.scalar(corner=Corner.MIN, edge=EdgeSelect.WORST))

    @pytest.mark.parametrize("wanted", ["u[0]", r"u\[0\]"])
    def test_instance_filter_matches_unescaped_name(self, wanted):
        result = query(_escaped_sdf(), instances=[wanted])
        assert list(result.cells["INV"]) == [r"u\[0\]"]

    @pytest.mark.parametrize(
        ("pattern", "kept"),
        [
            (r"^d\[1\]$", 1),
            (r"^u\[0\]/Y$", 1),
            (r"\\", 0),
            ("^d$", 0),
        ],
    )
    def test_pin_pattern_matches_unescaped_name(self, pattern, kept):
        result = query(_escaped_sdf(), pin_pattern=pattern)
        assert _count_entries(result.cells) == kept

    def test_pin_pattern_skips_entry_without_pins(self):
        device = Device(
            output=None,
            mode=DelayMode.ABSOLUTE,
            delay=DelayValues.of(Values.uniform(1)),
        )
        assert query(_one_cell(device), pin_pattern=".*").cells == {}

    @pytest.mark.parametrize("bounds", [{"min_delay": 0.0}, {"max_delay": 9.0}])
    def test_threshold_on_missing_corner_raises(self, bounds):
        typ_only = replace(
            _iopath(edge=None, delay=1.0),
            delay=DelayValues.of(Values(typ=1.0)),
        )
        with pytest.raises(MissingDelayError, match="DFF ff0"):
            query(_one_cell(typ_only), corner=Corner.MAX, **bounds)

    def test_filter_by_instance(self):
        sdf = _parse("spec-example1.sdf")
        first_instance = next(
            inst for instances in sdf.cells.values() for inst in instances
        )
        result = query(sdf, instances=[first_instance])
        for instances in result.cells.values():
            assert set(instances) == {first_instance}


class TestDiff:
    def test_identical_files(self):
        sdf = _parse("spec-example1.sdf")
        result = diff(sdf, sdf)
        assert result.only_in_a == []
        assert result.only_in_b == []
        assert result.value_diffs == []

    def test_different_files(self):
        result = diff(
            _parse("spec-example1.sdf"), _parse("test1.sdf"), normalize_first=True
        )
        assert result.only_in_a
        assert result.only_in_b

    def test_header_diffs(self):
        result = diff(
            _parse("spec-example1.sdf"), _parse("test1.sdf"), normalize_first=True
        )
        assert result.header_diffs[HeaderField.DESIGN] == ("system", None)

    @pytest.mark.parametrize(
        ("timescale_a", "timescale_b", "raises"),
        [("1ns", "1ps", True), ("1ns", None, False), ("10ps", "10 ps", False)],
    )
    def test_differing_timescales_raise_without_normalize(
        self, timescale_a, timescale_b, raises
    ):
        a, b = (
            replace(
                _one_cell(_iopath(edge=None, delay=1.0)),
                header=SDFHeader(timescale=None if ts is None else Timescale.parse(ts)),
            )
            for ts in (timescale_a, timescale_b)
        )
        if raises:
            with pytest.raises(ValueError, match="normalize_first"):
                diff(a, b)
        else:
            assert diff(a, b).value_diffs == []

    @pytest.mark.parametrize(
        ("entry_a", "entry_b", "expected"),
        [
            (
                _iopath(edge=None, delay=1.0),
                _iopath(edge=None, delay=2.0),
                [ValueField(slot=ValueSlot.DELAY, corner=c, rvalue=0) for c in Corner],
            ),
            (
                _setup(data=TimingPortSpec("D"), limit=1.0),
                _setup(data=TimingPortSpec("D"), limit=2.0),
                [ValueField(slot=ValueSlot.LIMIT, corner=c) for c in Corner],
            ),
            (
                replace(
                    _iopath(edge=None, delay=1.0),
                    delay=DelayValues(rvalues=(Values.uniform(1.0),) * 12),
                ),
                replace(
                    _iopath(edge=None, delay=1.0),
                    delay=DelayValues(rvalues=(Values.uniform(1.0),) * 2),
                ),
                [RvalueCountField()],
            ),
            (
                Slack(
                    port="D",
                    rise_setup=_ONE,
                    fall_setup=_ONE,
                    rise_hold=_ONE,
                    fall_hold=_ONE,
                ),
                Slack(
                    port="D",
                    rise_setup=_ONE,
                    fall_setup=_ONE,
                    rise_hold=_ONE,
                    fall_hold=Values(min=1.0, typ=1.0, max=2.0),
                    period=Decimal(10),
                ),
                [
                    ValueField(slot=ValueSlot.FALL_HOLD, corner=Corner.MAX),
                    *(ValueField(slot=ValueSlot.PERIOD, corner=c) for c in Corner),
                ],
            ),
        ],
        ids=["delay", "limit", "rvalue-count", "slack"],
    )
    def test_field_is_typed(self, entry_a, entry_b, expected):
        result = diff(_one_cell(entry_a), _one_cell(entry_b))
        assert [d.field for d in result.value_diffs] == expected

    def test_waveform_raises(self):
        edges = (
            WaveformEdge(edge=EdgeType.POSEDGE, offsets=(Decimal(0),)),
            WaveformEdge(edge=EdgeType.NEGEDGE, offsets=(Decimal(5),)),
        )
        sdf = _one_cell(Waveform(port="CLK", period=Decimal(10), edges=edges))
        with pytest.raises(UnsupportedDiffError, match="WAVEFORM CLK"):
            diff(sdf, sdf)

    @pytest.mark.parametrize(
        ("field", "text"),
        [
            (
                ValueField(slot=ValueSlot.DELAY, corner=Corner.MAX, rvalue=0),
                "delay[0].max",
            ),
            (ValueField(slot=ValueSlot.HOLD, corner=Corner.MIN), "hold.min"),
            (RvalueCountField(), "delay.count"),
        ],
    )
    def test_field_text(self, field, text):
        assert str(field) == text

    def test_only_in_entry_is_located(self):
        entry_a = _iopath(edge=EdgeType.POSEDGE, delay=1.0)
        entry_b = _iopath(edge=EdgeType.NEGEDGE, delay=1.0)
        result = diff(_one_cell(entry_a), _one_cell(entry_b))
        assert result.only_in_a == [
            EntryLocation(cell_type="DFF", instance="ff0", key=entry_a.key)
        ]
        assert result.only_in_b == [
            EntryLocation(cell_type="DFF", instance="ff0", key=entry_b.key)
        ]

    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            (("1ns", 1.0), ("1ps", 1000.0), ("1ns", "1ps")),
            (("100ps", 10.0), ("1ns", 1.0), ("100ps", "1ns")),
            (("1ns", 1.0), ("1ns", 1.0), None),
        ],
        ids=["ns-ps", "100ps-ns", "same"],
    )
    def test_normalize_keeps_original_timescale_in_header_diffs(self, a, b, expected):
        sdf_a, sdf_b = (
            replace(
                _one_cell(_iopath(edge=None, delay=delay)),
                header=SDFHeader(timescale=Timescale.parse(ts)),
            )
            for ts, delay in (a, b)
        )
        result = diff(sdf_a, sdf_b, normalize_first=True)
        assert result.value_diffs == []
        assert result.header_diffs.get(HeaderField.TIMESCALE) == expected

    def test_normalize_before_diff(self):
        sdf = _parse("spec-example1.sdf")
        result = diff(
            sdf,
            sdf,
            normalize_first=True,
            target_timescale=Timescale.parse("1ps"),
        )
        assert result.value_diffs == []

    @pytest.mark.parametrize(
        "name",
        ["spec-example1.sdf", "spec-example2.sdf", "spec-example3.sdf", "test1.sdf"],
    )
    def test_reordered_entries_give_no_diff(self, name):
        sdf = _parse(name)
        reordered = SDFFile(
            header=sdf.header,
            cells={
                cell_type: {
                    instance: entries[::-1] for instance, entries in instances.items()
                }
                for cell_type, instances in sdf.cells.items()
            },
        )
        assert reordered != sdf or _count_entries(sdf.cells) <= 1
        result = diff(sdf, reordered)
        assert result.only_in_a == []
        assert result.only_in_b == []
        assert result.value_diffs == []

    @pytest.mark.parametrize(
        ("pair_a", "pair_b"),
        [(a, b) for _, a, b in _DISTINCT_ARCS],
        ids=[name for name, _, _ in _DISTINCT_ARCS],
    )
    def test_distinct_arcs_compare_separately(self, pair_a, pair_b):
        result = diff(_one_cell(*pair_a), _one_cell(*pair_b))
        assert result.only_in_a == []
        assert result.only_in_b == []
        # Only the second arc changed, at every corner.
        assert {d.key for d in result.value_diffs} == {pair_a[1].key}
        assert len(result.value_diffs) == len(Corner)
        assert {(d.value_a, d.value_b) for d in result.value_diffs} == {(2.0, 3.0)}


class TestMerge:
    def test_merge_same_file(self):
        sdf = _parse("spec-example1.sdf")
        result = merge([sdf, sdf], strategy=ConflictStrategy.KEEP_LAST)
        assert result.cells == sdf.cells

    def test_merge_different_timescale_error(self):
        sdf_a = _parse("spec-example1.sdf")
        sdf_b = _parse("test1.sdf")
        assert sdf_a.header.timescale != sdf_b.header.timescale
        with pytest.raises(ValueError, match="timescale"):
            merge([sdf_a, sdf_b])

    def test_merge_with_normalization(self):
        result = merge(
            [_parse("spec-example1.sdf"), _parse("test1.sdf")],
            target_timescale=Timescale.parse("1ps"),
        )
        assert result.header.timescale == Timescale.parse("1ps")
        assert {"INV", "vpr_interconnect"} <= set(result.cells)

    @pytest.mark.parametrize("strategy", [None, ConflictStrategy.ERROR])
    def test_error_is_default_and_raises_on_differing_entry(self, strategy):
        first = _one_cell(_iopath(edge=None, delay=1.0))
        last = _one_cell(_iopath(edge=None, delay=2.0))
        kwargs = {} if strategy is None else {"strategy": strategy}
        with pytest.raises(MergeConflictError, match="Conflicting"):
            merge([first, last], **kwargs)

    @pytest.mark.parametrize(
        "name", ["spec-example1.sdf", "spec-example2.sdf", "test1.sdf"]
    )
    def test_error_accepts_identical_duplicates(self, name):
        sdf = _parse(name)
        assert merge([sdf, sdf], strategy=ConflictStrategy.ERROR).cells == sdf.cells

    @pytest.mark.parametrize(
        ("divider_a", "divider_b", "ok"),
        [
            (Divider.SLASH, Divider.DOT, False),
            (Divider.SLASH, None, False),
            (Divider.DOT, None, True),
            (Divider.SLASH, Divider.SLASH, True),
        ],
    )
    def test_divider_mismatch_raises(self, divider_a, divider_b, ok):
        files = [
            replace(_one_cell(), header=SDFHeader(divider=divider_a)),
            replace(_one_cell(), header=SDFHeader(divider=divider_b)),
        ]
        if ok:
            merged = merge(files)
            assert merged.header.effective_divider == files[0].header.effective_divider
        else:
            with pytest.raises(ValueError, match="DIVIDER"):
                merge(files)

    @pytest.mark.parametrize(
        ("design_a", "design_b", "expected"),
        [("A", "B", None), ("A", None, "A"), (None, "B", "B"), ("A", "A", "A")],
    )
    def test_design_mismatch_raises(self, design_a, design_b, expected):
        files = [
            replace(_one_cell(), header=SDFHeader(design=design_a)),
            replace(_one_cell(), header=SDFHeader(design=design_b)),
        ]
        if expected is None:
            with pytest.raises(ValueError, match="DESIGN"):
                merge(files)
        else:
            assert merge(files).header.design == expected

    @pytest.mark.parametrize(
        ("timescale_a", "timescale_b"),
        [("1ns", None), ("10 ps", "10ps"), ("1.0 us", "1us")],
    )
    def test_timescales_compare_by_value(self, timescale_a, timescale_b):
        files = [
            replace(
                _one_cell(),
                header=SDFHeader(timescale=None if ts is None else Timescale.parse(ts)),
            )
            for ts in (timescale_a, timescale_b)
        ]
        merged = merge(files)
        assert merged.header.effective_timescale == Timescale.parse(timescale_a)

    @pytest.mark.parametrize(
        ("strategy", "expected"),
        [(ConflictStrategy.KEEP_FIRST, 1.0), (ConflictStrategy.KEEP_LAST, 2.0)],
    )
    def test_conflict_strategy_picks_entry(self, strategy, expected):
        first = _one_cell(_iopath(edge=None, delay=1.0))
        last = _one_cell(_iopath(edge=None, delay=2.0))
        result = merge([first, last], strategy=strategy)
        assert result.cells["DFF"]["ff0"] == [_iopath(edge=None, delay=expected)]

    @pytest.mark.parametrize(
        ("pair_a", "pair_b"),
        [(a, b) for _, a, b in _DISTINCT_ARCS],
        ids=[name for name, _, _ in _DISTINCT_ARCS],
    )
    def test_distinct_arcs_do_not_conflict(self, pair_a, pair_b):
        result = merge(
            [_one_cell(pair_a[0]), _one_cell(pair_b[1])],
            strategy=ConflictStrategy.ERROR,
        )
        assert result.cells["DFF"]["ff0"] == [pair_a[0], pair_b[1]]

    def test_merge_empty_raises(self):
        with pytest.raises(ValueError, match="No files"):
            merge([])


class TestBatchEndpointAnalysis:
    def test_basic_analysis(self, spec2_graph):
        results = batch_endpoint_analysis(spec2_graph, corner=Corner.MIN)
        assert len(results) > 0
        delays = [r.critical_delay for r in results if r.critical_delay is not None]
        assert delays == sorted(delays, reverse=True)

    def test_with_specific_sources(self, spec2_graph):
        source = sorted(spec2_graph.startpoints())[0]
        results = batch_endpoint_analysis(
            spec2_graph, sources=[source], corner=Corner.MIN
        )
        assert results
        for r in results:
            assert r.source == source

    def test_path_count_positive(self, spec2_graph):
        results = batch_endpoint_analysis(spec2_graph, corner=Corner.MIN)
        for r in results:
            assert r.path_count > 0


class TestReport:
    def test_basic_report(self):
        text = generate_report(_parse("spec-example2.sdf"), corner=Corner.MIN)
        assert "SDF Header" in text
        assert "Statistics" in text

    def test_report_with_period(self):
        text = generate_report(
            _parse("spec-example2.sdf"), corner=Corner.MIN, period=Decimal(10)
        )
        assert "Slack" in text

    def test_report_with_validation_issues(self):
        sdf = SDFFile(header=SDFHeader(), cells={"A": {"a0": []}})
        text = generate_report(sdf, corner=Corner.MIN)
        assert "Validation Issues" in text

    def test_report_counts_delays_without_value(self):
        typ_only = replace(
            _iopath(edge=None, delay=1.0), delay=DelayValues.of(Values(typ=1.0))
        )
        text = generate_report(_one_cell(typ_only), corner=Corner.MAX)
        assert "Delays Without Value" in text

    def test_report_format_delay_none(self):
        assert _format_delay(None) == "N/A"
        assert _format_delay(Decimal("1.5")) == "1.500000"
