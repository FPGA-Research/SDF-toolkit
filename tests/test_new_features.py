import pytest
from conftest import DATA_DIR

from sdf_toolkit.analysis.diff import diff
from sdf_toolkit.analysis.query import query
from sdf_toolkit.analysis.report import _format_float, generate_report
from sdf_toolkit.analysis.stats import compute_stats
from sdf_toolkit.analysis.validate import validate
from sdf_toolkit.core.builder import SDFBuilder
from sdf_toolkit.core.model import (
    BaseEntry,
    CellsDict,
    DelayPaths,
    EntryType,
    SDFFile,
    SDFHeader,
    Values,
)
from sdf_toolkit.core.pathgraph import batch_endpoint_analysis
from sdf_toolkit.parser.parser import parse_sdf
from sdf_toolkit.transform.merge import ConflictStrategy, merge
from sdf_toolkit.transform.normalize import normalize_delays


def _count_entries(cells: CellsDict) -> int:
    return sum(
        len(entries) for instances in cells.values() for entries in instances.values()
    )


class TestNormalize:
    def test_normalize_ps_to_ns(self):
        sdf = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        result = normalize_delays(sdf, "1ns")
        assert result.header.timescale == "1ns"
        # Values should be scaled by 1000/1000000 = 0.001
        # Original delay 286 ps -> 0.286 ns (slow.min field)
        first_cell = next(iter(next(iter(result.cells.values())).values()))
        first_entry = next(iter(first_cell.values()))
        assert first_entry.delay_paths is not None
        assert first_entry.delay_paths.slow is not None
        assert abs(first_entry.delay_paths.slow.min - 0.286) < 1e-9

    def test_normalize_ns_to_ps(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = normalize_delays(sdf, "1ps")
        assert result.header.timescale == "1ps"
        # Check that original is unmodified
        assert sdf.header.timescale == "1ns"

    def test_normalize_no_timescale_raises(self):
        sdf = SDFFile(header=SDFHeader(), cells={})
        with pytest.raises(ValueError, match="no timescale"):
            normalize_delays(sdf, "1ns")

    def test_normalize_same_timescale(self):
        sdf = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        result = normalize_delays(sdf, "1ps")
        # Ratio should be 1.0 (same timescale: 1ps -> 1ps)
        first_cell = next(iter(next(iter(result.cells.values())).values()))
        first_entry = next(iter(first_cell.values()))
        orig_cell = next(iter(next(iter(sdf.cells.values())).values()))
        orig_entry = next(iter(orig_cell.values()))
        assert first_entry.delay_paths.slow.min == orig_entry.delay_paths.slow.min

    def test_original_unmodified(self):
        sdf = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        original_ts = sdf.header.timescale
        normalize_delays(sdf, "1ns")
        assert sdf.header.timescale == original_ts


class TestBuilder:
    def test_basic_build(self):
        sdf = (
            SDFBuilder()
            .set_header(sdfversion="3.0", timescale="1ns")
            .add_cell("BUF", "buf0")
            .add_iopath("A", "Y", {"nominal": {"min": 1.0, "avg": 2.0, "max": 3.0}})
            .build()
        )
        assert sdf.header.sdfversion == "3.0"
        assert "BUF" in sdf.cells
        assert "buf0" in sdf.cells["BUF"]
        entry = sdf.cells["BUF"]["buf0"]["iopath_A_Y"]
        assert entry.delay_paths.nominal.min == 1.0

    def test_multiple_cells(self):
        sdf = (
            SDFBuilder()
            .add_cell("BUF", "b0")
            .add_iopath("A", "Y", {"nominal": {"min": 1.0, "avg": 1.0, "max": 1.0}})
            .add_cell("INV", "i0")
            .add_iopath("A", "Z", {"nominal": {"min": 2.0, "avg": 2.0, "max": 2.0}})
            .build()
        )
        assert len(sdf.cells) == 2

    def test_collision_safe_naming(self):
        sdf = (
            SDFBuilder()
            .add_cell("BUF", "b0")
            .add_iopath("A", "Y", {"nominal": {"min": 1.0, "avg": 1.0, "max": 1.0}})
            .add_iopath("A", "Y", {"nominal": {"min": 2.0, "avg": 2.0, "max": 2.0}})
            .build()
        )
        entries = sdf.cells["BUF"]["b0"]
        assert "iopath_A_Y" in entries
        assert "iopath_A_Y_1" in entries

    def test_timing_checks(self):
        sdf = (
            SDFBuilder()
            .add_cell("FF", "ff0")
            .add_setup("D", "CLK", {"nominal": {"min": 0.5, "avg": 0.5, "max": 0.5}})
            .add_hold("D", "CLK", {"nominal": {"min": 0.3, "avg": 0.3, "max": 0.3}})
            .build()
        )
        entries = sdf.cells["FF"]["ff0"]
        assert entries["setup_D_CLK"].is_timing_check
        assert entries["hold_D_CLK"].is_timing_check

    def test_all_entry_types(self):
        delays = {"nominal": {"min": 1.0, "avg": 1.0, "max": 1.0}}
        builder = SDFBuilder().add_cell("TOP", "top0")
        builder = (
            builder.add_iopath("A", "Y", delays)
            .add_interconnect("P1", "P2", delays)
            .add_port("P1", delays)
            .add_device("D1", delays)
            .add_setup("D", "CLK", delays)
            .add_hold("D", "CLK", delays)
            .add_removal("RST", "CLK", delays)
            .add_recovery("RST", "CLK", delays)
            .add_setuphold("D", "CLK", delays)
            .add_width("CLK", delays)
            .add_period("CLK", delays)
        )
        sdf = builder.build()
        assert len(sdf.cells["TOP"]["top0"]) == 11


class TestValidate:
    def test_valid_file_no_issues(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        issues = validate(sdf)
        # spec-example1 is a well-formed file, may have minor warnings
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 0

    def test_missing_timescale_warning(self):
        sdf = SDFFile(header=SDFHeader(), cells={"A": {"a0": {}}})
        issues = validate(sdf)
        assert any("timescale" in i.message.lower() for i in issues)

    def test_empty_cells_warning(self):
        sdf = SDFFile(header=SDFHeader(timescale="1ps"), cells={})
        issues = validate(sdf)
        assert any("no cells" in i.message.lower() for i in issues)

    def test_none_delay_paths_error(self):
        sdf = SDFFile(
            header=SDFHeader(timescale="1ps"),
            cells={"A": {"a0": {"e1": BaseEntry(name="e1", delay_paths=None)}}},
        )
        issues = validate(sdf)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) >= 1

    def test_errors_sorted_first(self):
        sdf = SDFFile(
            header=SDFHeader(),
            cells={"A": {"a0": {"e1": BaseEntry(name="e1", delay_paths=None)}}},
        )
        issues = validate(sdf)
        # Verify errors appear before warnings in the sorted list
        severities = [issue.severity for issue in issues]
        assert len(severities) >= 2
        assert severities == sorted(severities, key=lambda s: 0 if s == "error" else 1)

    def test_missing_from_pin(self):
        entry = BaseEntry(
            name="iopath_none_Y",
            type=EntryType.IOPATH,
            from_pin=None,
            to_pin="Y",
            delay_paths=DelayPaths(nominal=Values(min=1.0, avg=1.0, max=1.0)),
        )
        sdf = SDFFile(
            header=SDFHeader(timescale="1ps"),
            cells={"A": {"a0": {"e1": entry}}},
        )
        issues = validate(sdf)
        errors = [i for i in issues if i.severity == "error"]
        assert any("from_pin" in i.message for i in errors)

    def test_missing_to_pin(self):
        entry = BaseEntry(
            name="iopath_A_none",
            type=EntryType.IOPATH,
            from_pin="A",
            to_pin=None,
            delay_paths=DelayPaths(nominal=Values(min=1.0, avg=1.0, max=1.0)),
        )
        sdf = SDFFile(
            header=SDFHeader(timescale="1ps"),
            cells={"A": {"a0": {"e1": entry}}},
        )
        issues = validate(sdf)
        errors = [i for i in issues if i.severity == "error"]
        assert any("to_pin" in i.message for i in errors)

    def test_cross_cell_type_instance_warning(self):
        dp = DelayPaths(nominal=Values(min=1.0, avg=1.0, max=1.0))
        entry_a = BaseEntry(
            name="e1",
            type=EntryType.IOPATH,
            from_pin="A",
            to_pin="Y",
            delay_paths=dp,
        )
        entry_b = BaseEntry(
            name="e2",
            type=EntryType.IOPATH,
            from_pin="A",
            to_pin="Y",
            delay_paths=dp,
        )
        sdf = SDFFile(
            header=SDFHeader(timescale="1ps"),
            cells={
                "BUF": {"shared_inst": {"e1": entry_a}},
                "INV": {"shared_inst": {"e2": entry_b}},
            },
        )
        issues = validate(sdf)
        assert any("multiple" in i.message.lower() for i in issues)


class TestStats:
    def test_stats_from_file(self):
        sdf = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        # test1.sdf uses slow/fast fields, not nominal
        stats = compute_stats(sdf, field="slow", metric="min")
        assert stats.total_cells > 0
        assert stats.total_instances > 0
        assert stats.total_entries > 0
        assert stats.delay_min is not None
        assert stats.delay_max is not None
        assert stats.delay_min <= stats.delay_max

    def test_stats_empty_sdf(self):
        sdf = SDFFile()
        stats = compute_stats(sdf)
        assert stats.total_cells == 0
        assert stats.total_entries == 0
        assert stats.delay_min is None
        assert stats.delay_mean is None

    def test_stats_entry_type_counts(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        stats = compute_stats(sdf, field="slow", metric="min")
        has_expected_type = (
            "iopath" in stats.entry_type_counts
            or "interconnect" in stats.entry_type_counts
        )
        assert has_expected_type


class TestQuery:
    def test_filter_by_cell_type(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = query(sdf, cell_types=["INV"])
        assert "INV" in result.cells
        assert "OR2" not in result.cells

    def test_filter_by_entry_type(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = query(sdf, entry_types=[EntryType.IOPATH])
        for instances in result.cells.values():
            for entries in instances.values():
                for entry in entries.values():
                    assert entry.type == EntryType.IOPATH

    def test_filter_by_pin_pattern(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = query(sdf, pin_pattern="^z$")
        # All entries should have 'z' in from_pin or to_pin
        for instances in result.cells.values():
            for entries in instances.values():
                for entry in entries.values():
                    assert entry.from_pin == "z" or entry.to_pin == "z"

    def test_no_filters_returns_all(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = query(sdf)
        assert _count_entries(result.cells) == _count_entries(sdf.cells)

    def test_filter_preserves_header(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = query(sdf, cell_types=["INV"])
        assert result.header.timescale == sdf.header.timescale

    def test_delay_threshold_filter(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = query(sdf, min_delay=0.3, field="slow", metric="min")
        for instances in result.cells.values():
            for entries in instances.values():
                for entry in entries.values():
                    if entry.delay_paths:
                        scalar = entry.delay_paths.get_scalar("slow", "min")
                        if scalar is not None:
                            assert scalar >= 0.3

    def test_filter_by_instance(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        # spec-example1 has instances; pick one that exists
        first_instance = next(
            inst for instances in sdf.cells.values() for inst in instances
        )
        result = query(sdf, instances=[first_instance])
        for instances in result.cells.values():
            assert first_instance in instances

    def test_max_delay_filter(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = query(sdf, max_delay=0.5, field="slow", metric="max")
        for instances in result.cells.values():
            for entries in instances.values():
                for entry in entries.values():
                    if entry.delay_paths:
                        scalar = entry.delay_paths.get_scalar("slow", "max")
                        if scalar is not None:
                            assert scalar <= 0.5


class TestDiff:
    def test_identical_files(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = diff(sdf, sdf)
        assert len(result.only_in_a) == 0
        assert len(result.only_in_b) == 0
        assert len(result.value_diffs) == 0

    def test_different_files(self):
        sdf_a = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        sdf_b = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        result = diff(sdf_a, sdf_b)
        assert (
            len(result.only_in_a) > 0
            or len(result.only_in_b) > 0
            or len(result.header_diffs) > 0
        )

    def test_header_diffs(self):
        sdf_a = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        sdf_b = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        result = diff(sdf_a, sdf_b)
        # Different timescales: 1ns vs 1ps
        assert "timescale" in result.header_diffs

    def test_normalize_before_diff(self):
        sdf_a = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        sdf_b = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = diff(sdf_a, sdf_b, normalize_first=True, target_timescale="1ps")
        assert len(result.value_diffs) == 0


class TestMerge:
    def test_merge_same_file(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = merge([sdf, sdf], strategy=ConflictStrategy.KEEP_LAST)
        assert len(result.cells) > 0

    def test_merge_different_timescale_error(self):
        sdf_a = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        sdf_b = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        with pytest.raises(ValueError, match="timescale"):
            merge([sdf_a, sdf_b])

    def test_merge_with_normalization(self):
        sdf_a = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        sdf_b = parse_sdf((DATA_DIR / "test1.sdf").read_text())
        result = merge([sdf_a, sdf_b], target_timescale="1ps")
        assert result.header.timescale == "1ps"
        # Should have cells from both files
        assert len(result.cells) >= 1

    def test_merge_conflict_error(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        with pytest.raises(ValueError, match="Conflicting"):
            merge([sdf, sdf], strategy=ConflictStrategy.ERROR)

    def test_merge_keep_first(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        result = merge([sdf, sdf], strategy=ConflictStrategy.KEEP_FIRST)
        assert len(result.cells) > 0

    def test_merge_empty_raises(self):
        with pytest.raises(ValueError, match="No files"):
            merge([])


class TestBatchEndpointAnalysis:
    def test_basic_analysis(self, spec1_graph):
        results = batch_endpoint_analysis(spec1_graph, field="slow", metric="min")
        assert len(results) > 0
        # Results should be sorted by critical_delay descending
        delays = [r.critical_delay for r in results if r.critical_delay is not None]
        assert delays == sorted(delays, reverse=True)

    def test_with_specific_sources(self, spec1_graph):
        starts = sorted(spec1_graph.startpoints())
        if starts:
            results = batch_endpoint_analysis(
                spec1_graph,
                sources=[starts[0]],
                field="slow",
                metric="min",
            )
            for r in results:
                assert r.source == starts[0]

    def test_path_count_positive(self, spec1_graph):
        results = batch_endpoint_analysis(spec1_graph, field="slow", metric="min")
        for r in results:
            assert r.path_count > 0


class TestReport:
    def test_basic_report(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        text = generate_report(sdf, field="slow", metric="min")
        assert "SDF Header" in text
        assert "Statistics" in text

    def test_report_with_period(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        text = generate_report(sdf, field="slow", metric="min", period=10.0)
        assert "Slack" in text

    def test_report_returns_string(self):
        sdf = parse_sdf((DATA_DIR / "spec-example1.sdf").read_text())
        text = generate_report(sdf, field="slow", metric="min")
        assert isinstance(text, str)
        assert len(text) > 0

    def test_report_with_validation_issues(self):
        """Report on SDF with validation issues shows the issues table."""
        sdf = SDFFile(
            header=SDFHeader(),
            cells={"A": {"a0": {"e1": BaseEntry(name="e1", delay_paths=None)}}},
        )
        text = generate_report(sdf, field="slow", metric="min")
        assert "Validation Issues" in text

    def test_report_format_float_none(self):
        """Exercise _format_float(None) -> 'N/A'."""
        assert _format_float(None) == "N/A"
        assert "." in _format_float(1.5)
