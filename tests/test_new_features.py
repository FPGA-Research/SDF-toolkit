from dataclasses import replace

import pytest
from conftest import DATA_DIR

from sdf_toolkit.analysis.diff import diff
from sdf_toolkit.analysis.query import query
from sdf_toolkit.analysis.report import _format_float, generate_report
from sdf_toolkit.analysis.stats import compute_stats
from sdf_toolkit.analysis.validate import validate
from sdf_toolkit.core.model import (
    CellsDict,
    Cond,
    Corner,
    DelayMode,
    DelayValues,
    Device,
    Divider,
    EdgeSelect,
    EdgeType,
    Entry,
    EntryType,
    HeaderField,
    Interconnect,
    Iopath,
    Port,
    PortSpec,
    SDFFile,
    SDFHeader,
    Setup,
    Timescale,
    TimingPortSpec,
    Values,
)
from sdf_toolkit.core.pathgraph import batch_endpoint_analysis
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


class TestNormalize:
    def test_normalize_ps_to_ns(self):
        sdf = _parse("test1.sdf")
        result = normalize_delays(sdf, Timescale.parse("1ns"))
        assert result.header.timescale == Timescale.parse("1ns")
        # 286 ps is 0.286 ns.
        assert _first_delay(result).rvalues[0].min == pytest.approx(0.286)

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

    def test_empty_triple_warning(self):
        sdf = _one_cell(
            _iopath(edge=None, delay=1.0),
            Iopath(
                input=PortSpec("A"),
                output=PortSpec("Y"),
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(Values.uniform(1.0), Values()),
            ),
        )
        [issue] = validate(sdf)
        assert issue.message == "'delay[1]' is empty: no min, typ or max value"
        assert issue.entry_name == "ABSOLUTE IOPATH A Y"

    def test_cross_cell_type_instance_warning(self):
        entry = _iopath(edge=None, delay=1.0)
        sdf = SDFFile(
            header=SDFHeader(timescale=Timescale.parse("1ps")),
            cells={
                "BUF": {"shared_inst": [entry]},
                "INV": {"shared_inst": [entry]},
            },
        )
        issues = validate(sdf)
        assert any("multiple" in i.message.lower() for i in issues)


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
        assert "iopath" in stats.entry_type_counts
        assert "interconnect" in stats.entry_type_counts


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
        result = diff(_parse("spec-example1.sdf"), _parse("test1.sdf"))
        assert result.only_in_a
        assert result.only_in_b

    def test_header_diffs(self):
        result = diff(_parse("spec-example1.sdf"), _parse("test1.sdf"))
        assert result.header_diffs[HeaderField.TIMESCALE] == ("1ns", "1ps")

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
    def test_basic_analysis(self, spec1_graph):
        results = batch_endpoint_analysis(spec1_graph, corner=Corner.MIN)
        assert len(results) > 0
        delays = [r.critical_delay for r in results if r.critical_delay is not None]
        assert delays == sorted(delays, reverse=True)

    def test_with_specific_sources(self, spec1_graph):
        source = sorted(spec1_graph.startpoints())[0]
        results = batch_endpoint_analysis(
            spec1_graph, sources=[source], corner=Corner.MIN
        )
        assert results
        for r in results:
            assert r.source == source

    def test_path_count_positive(self, spec1_graph):
        results = batch_endpoint_analysis(spec1_graph, corner=Corner.MIN)
        for r in results:
            assert r.path_count > 0


class TestReport:
    def test_basic_report(self):
        text = generate_report(_parse("spec-example1.sdf"), corner=Corner.MIN)
        assert "SDF Header" in text
        assert "Statistics" in text

    def test_report_with_period(self):
        text = generate_report(
            _parse("spec-example1.sdf"), corner=Corner.MIN, period=10.0
        )
        assert "Slack" in text

    def test_report_with_validation_issues(self):
        sdf = SDFFile(header=SDFHeader(), cells={"A": {"a0": []}})
        text = generate_report(sdf, corner=Corner.MIN)
        assert "Validation Issues" in text

    def test_report_format_float_none(self):
        assert _format_float(None) == "N/A"
        assert "." in _format_float(1.5)
