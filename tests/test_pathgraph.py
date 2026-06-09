import networkx as nx
import pytest

from sdf_toolkit.core.builder import SDFBuilder
from sdf_toolkit.core.model import (
    BaseEntry,
    DelayPaths,
    EntryType,
    SDFFile,
    SDFHeader,
    Values,
)
from sdf_toolkit.core.pathgraph import (
    TimingGraph,
    VerificationResult,
    decompose_delay,
    rank_paths,
    verify_path,
)

EXPECTED_NODES = {
    "P1/z",
    "B1/C1/i",
    "B1/C1/z",
    "B1/C2/i1",
    "B1/C2/i2",
    "B1/C2/z",
    "B2/C1/i",
    "B2/C1/z",
    "B2/C2/i1",
    "B2/C2/i2",
    "B2/C2/z",
    "D1/i",
    "D1/z",
    "P2/i",
    "P3/i",
}


class TestTimingGraphBuild:
    def test_nodes(self, spec1_graph: TimingGraph) -> None:
        nodes = spec1_graph.nodes()
        assert nodes == EXPECTED_NODES

    def test_edge_count(self, spec1_graph: TimingGraph) -> None:
        edges = spec1_graph.edges()
        assert len(edges) == 16

    def test_successors(self, spec1_graph: TimingGraph) -> None:
        succs = spec1_graph.successors("P1/z")
        sink_names = {e.sink for e in succs}
        assert sink_names == {"B1/C1/i", "B1/C2/i2"}

    def test_predecessors(self, spec1_graph: TimingGraph) -> None:
        preds = spec1_graph.predecessors("P2/i")
        source_names = {e.source for e in preds}
        assert source_names == {"B2/C2/z"}

    def test_graph_property(self, spec1_graph: TimingGraph) -> None:
        assert isinstance(spec1_graph.graph, nx.MultiDiGraph)


class TestFindPaths:
    def test_find_paths_p1_to_p2(self, spec1_graph: TimingGraph) -> None:
        paths = spec1_graph.find_paths("P1/z", "P2/i")
        assert len(paths) == 2

    def test_find_paths_no_path(self, spec1_graph: TimingGraph) -> None:
        paths = spec1_graph.find_paths("P2/i", "P1/z")
        assert paths == []


class TestComposeDelay:
    def test_compose_single_edge(self, spec1_graph: TimingGraph) -> None:
        succs = spec1_graph.successors("P1/z")
        edge_to_c1 = [e for e in succs if e.sink == "B1/C1/i"][0]
        result = spec1_graph.compose_delay([edge_to_c1])
        assert result.approx_eq(edge_to_c1.delay)

    def test_compose_p1_to_p2(self, spec1_graph: TimingGraph) -> None:
        paths = spec1_graph.find_paths("P1/z", "P2/i")
        delays = [spec1_graph.compose_delay(p) for p in paths]

        expected_path1 = DelayPaths(
            fast=Values(min=1.805, avg=None, max=1.805),
            slow=Values(min=1.795, avg=None, max=1.795),
        )
        expected_path2 = DelayPaths(
            fast=Values(min=1.355, avg=None, max=1.355),
            slow=Values(min=1.380, avg=None, max=1.380),
        )

        assert any(expected_path1.approx_eq(d, tolerance=1e-6) for d in delays)
        assert any(expected_path2.approx_eq(d, tolerance=1e-6) for d in delays)

    def test_compose_empty_path(self, spec1_graph: TimingGraph) -> None:
        with pytest.raises(ValueError, match="empty path"):
            spec1_graph.compose_delay([])


class TestVerifyPath:
    def test_verify_pass(self, spec1_graph: TimingGraph) -> None:
        expected = DelayPaths(
            fast=Values(min=1.805, avg=None, max=1.805),
            slow=Values(min=1.795, avg=None, max=1.795),
        )
        result = verify_path(spec1_graph, "P1/z", "P2/i", expected, tolerance=1e-6)
        assert isinstance(result, VerificationResult)
        assert result.passed is True
        assert result.source == "P1/z"
        assert result.sink == "P2/i"

    def test_verify_fail(self, spec1_graph: TimingGraph) -> None:
        expected = DelayPaths(
            nominal=Values(min=999.0, avg=None, max=999.0),
            fast=Values(min=999.0, avg=None, max=999.0),
        )
        result = verify_path(spec1_graph, "P1/z", "P2/i", expected)
        assert result.passed is False


class TestDecomposeDelay:
    def test_decompose(self) -> None:
        total = DelayPaths(
            nominal=Values(min=1.805, avg=None, max=1.805),
            fast=Values(min=1.795, avg=None, max=1.795),
        )
        known = DelayPaths(
            nominal=Values(min=1.0, avg=None, max=1.0),
            fast=Values(min=1.0, avg=None, max=1.0),
        )
        unknown = decompose_delay(total, known)
        expected = DelayPaths(
            nominal=Values(min=0.805, avg=None, max=0.805),
            fast=Values(min=0.795, avg=None, max=0.795),
        )
        assert unknown.approx_eq(expected, tolerance=1e-9)


class TestGraphSkipsNonRoutableEntries:
    """TimingGraph should skip entries that are not IOPATH or INTERCONNECT."""

    def test_setup_hold_entries_skipped(self) -> None:
        sdf = (
            SDFBuilder()
            .set_header(timescale="1ps")
            .add_cell("FF", "ff0")
            .add_setup("D", "CLK", {"nominal": {"min": 0.5, "avg": 0.5, "max": 0.5}})
            .add_hold("D", "CLK", {"nominal": {"min": 0.3, "avg": 0.3, "max": 0.3}})
            .add_iopath("D", "Q", {"nominal": {"min": 1.0, "avg": 1.0, "max": 1.0}})
            .build()
        )
        graph = TimingGraph(sdf)
        # Only the IOPATH should create edges; SETUP/HOLD are skipped
        assert len(graph.edges()) == 1

    def test_entries_with_none_pins_skipped(self) -> None:
        sdf = SDFFile(
            header=SDFHeader(timescale="1ps"),
            cells={
                "A": {
                    "a0": {
                        "e1": BaseEntry(
                            name="e1",
                            type=EntryType.IOPATH,
                            from_pin=None,
                            to_pin="Y",
                            delay_paths=DelayPaths(
                                nominal=Values(1.0, 1.0, 1.0),
                            ),
                        )
                    }
                }
            },
        )
        graph = TimingGraph(sdf)
        assert len(graph.edges()) == 0

    def test_entries_with_none_delay_paths_skipped(self) -> None:
        sdf = SDFFile(
            header=SDFHeader(timescale="1ps"),
            cells={
                "A": {
                    "a0": {
                        "e1": BaseEntry(
                            name="e1",
                            type=EntryType.IOPATH,
                            from_pin="A",
                            to_pin="Y",
                            delay_paths=None,
                        )
                    }
                }
            },
        )
        graph = TimingGraph(sdf)
        assert len(graph.edges()) == 0


class TestRankPathsNoneScalar:
    def test_rank_paths_with_nonexistent_field(self, spec1_graph: TimingGraph) -> None:
        """rank_paths with a field that yields None scalar still sorts correctly."""
        ranked = rank_paths(spec1_graph, "P1/z", "P2/i", field="nominal", metric="min")
        # spec-example1 has fast/slow only, so nominal yields None
        assert len(ranked) > 0
        for rp in ranked:
            assert rp.scalar is None


class TestEmptyGraph:
    def test_empty_sdf(self) -> None:
        sdf = SDFFile(header=SDFHeader(), cells={})
        graph = TimingGraph(sdf)
        assert graph.nodes() == set()
        assert graph.edges() == []


class TestParallelEdges:
    """Bug #1: find_paths overcounts when parallel edges exist."""

    @pytest.fixture
    def parallel_graph(self) -> TimingGraph:
        """Graph with two parallel IOPATH edges from A to Y in one cell."""
        sdf = (
            SDFBuilder()
            .set_header(timescale="1ps")
            .add_cell("BUF", "b0")
            .add_iopath(
                "A",
                "Y",
                {"slow": {"min": 1.0, "avg": 2.0, "max": 3.0}},
            )
            .add_iopath(
                "A",
                "Y",
                {"slow": {"min": 4.0, "avg": 5.0, "max": 6.0}},
            )
            .build()
        )
        return TimingGraph(sdf)

    def test_find_paths_count_with_parallel_edges(
        self, parallel_graph: TimingGraph
    ) -> None:
        """Two parallel edges s->t should give exactly 2 paths, not 4."""
        paths = parallel_graph.find_paths("b0/A", "b0/Y")
        assert len(paths) == 2

    def test_find_paths_no_duplicate_delays(
        self, parallel_graph: TimingGraph
    ) -> None:
        """Each parallel edge path should have a distinct delay."""
        paths = parallel_graph.find_paths("b0/A", "b0/Y")
        delays = [parallel_graph.compose_delay(p) for p in paths]
        scalars = sorted(
            d.get_scalar("slow", "max") for d in delays  # type: ignore[type-var]
        )
        assert scalars == [3.0, 6.0]

    def test_rank_paths_count_with_parallel_edges(
        self, parallel_graph: TimingGraph
    ) -> None:
        ranked = rank_paths(parallel_graph, "b0/A", "b0/Y", "slow", "max")
        assert len(ranked) == 2

    def test_critical_path_with_parallel_edges(
        self, parallel_graph: TimingGraph
    ) -> None:
        from sdf_toolkit.core.pathgraph import critical_path

        cp = critical_path(parallel_graph, "b0/A", "b0/Y", "slow", "max")
        assert cp is not None
        assert cp.scalar == 6.0

    def test_batch_endpoint_path_count_with_parallel_edges(
        self, parallel_graph: TimingGraph
    ) -> None:
        from sdf_toolkit.core.pathgraph import batch_endpoint_analysis

        results = batch_endpoint_analysis(parallel_graph, "slow", "max")
        assert len(results) == 1
        assert results[0].path_count == 2


class TestParallelEdgesMultiHop:
    """Parallel edges on multi-hop paths."""

    @pytest.fixture
    def multi_hop_parallel_graph(self) -> TimingGraph:
        """a/Y --(2 edges)--> b/A -> b/Y with 1 edge."""
        sdf = (
            SDFBuilder()
            .set_header(timescale="1ps")
            .add_cell("BUF", "b")
            .add_iopath("A", "Y", {"slow": {"min": 1.0, "avg": 1.0, "max": 1.0}})
            .add_interconnect(
                "a/Y", "b/A", {"slow": {"min": 2.0, "avg": 2.0, "max": 2.0}}
            )
            .add_interconnect(
                "a/Y", "b/A", {"slow": {"min": 3.0, "avg": 3.0, "max": 3.0}}
            )
            .build()
        )
        return TimingGraph(sdf)

    def test_multi_hop_parallel_count(
        self, multi_hop_parallel_graph: TimingGraph
    ) -> None:
        """2 parallel edges on first hop * 1 edge on second = 2 paths."""
        paths = multi_hop_parallel_graph.find_paths("a/Y", "b/Y")
        assert len(paths) == 2


class TestNoneScalarSorting:
    """Bug #3: None scalars should sort last in rank_paths, not first."""

    @pytest.fixture
    def mixed_field_graph(self) -> TimingGraph:
        """Graph where one path yields a scalar and another yields None."""
        sdf = (
            SDFBuilder()
            .set_header(timescale="1ps")
            .add_cell("BUF", "b1")
            .add_iopath("A", "Y", {"slow": {"min": 1.0, "avg": 2.0, "max": 3.0}})
            .add_cell("BUF", "b2")
            .add_iopath("A", "Y", {"fast": {"min": 10.0, "avg": 20.0, "max": 30.0}})
            .add_interconnect(
                "src/Y", "b1/A", {"slow": {"min": 0.5, "avg": 0.5, "max": 0.5}}
            )
            .add_interconnect(
                "src/Y", "b2/A", {"fast": {"min": 0.5, "avg": 0.5, "max": 0.5}}
            )
            .add_interconnect(
                "b1/Y", "sink/A", {"slow": {"min": 0.5, "avg": 0.5, "max": 0.5}}
            )
            .add_interconnect(
                "b2/Y", "sink/A", {"fast": {"min": 0.5, "avg": 0.5, "max": 0.5}}
            )
            .build()
        )
        return TimingGraph(sdf)

    def test_none_scalars_sort_last_descending(
        self, mixed_field_graph: TimingGraph
    ) -> None:
        """When ranking by 'slow'/'max', paths with None scalar go last."""
        ranked = rank_paths(
            mixed_field_graph, "src/Y", "sink/A", "slow", "max", descending=True
        )
        assert len(ranked) == 2
        # First path should have a real scalar, second should be None
        assert ranked[0].scalar is not None
        assert ranked[1].scalar is None

    def test_none_scalars_sort_last_ascending(
        self, mixed_field_graph: TimingGraph
    ) -> None:
        """When ascending, None scalars still go last."""
        ranked = rank_paths(
            mixed_field_graph, "src/Y", "sink/A", "slow", "max", descending=False
        )
        assert len(ranked) == 2
        assert ranked[-1].scalar is None

    def test_critical_path_skips_none_scalar(
        self, mixed_field_graph: TimingGraph
    ) -> None:
        """critical_path should return a path with a real scalar, not None."""
        from sdf_toolkit.core.pathgraph import critical_path

        cp = critical_path(mixed_field_graph, "src/Y", "sink/A", "slow", "max")
        assert cp is not None
        assert cp.scalar is not None
        assert cp.scalar == pytest.approx(4.0, abs=1e-6)


class TestMixedFieldComposition:
    """DelayPaths intersection semantics: composition of mixed fields yields None."""

    def test_compose_mixed_fields_yields_none(self) -> None:
        """Adding DelayPaths with disjoint fields gives empty result."""
        a = DelayPaths(slow=Values(min=1.0, avg=2.0, max=3.0))
        b = DelayPaths(nominal=Values(min=1.0, avg=2.0, max=3.0))
        result = a + b
        # Intersection-based: neither field is in both, so both are None
        assert result.slow is None
        assert result.nominal is None

    def test_compose_overlapping_fields_keeps_overlap(self) -> None:
        """Adding DelayPaths with overlapping fields keeps the overlap."""
        a = DelayPaths(
            slow=Values(min=1.0, avg=2.0, max=3.0),
            fast=Values(min=0.5, avg=1.0, max=1.5),
        )
        b = DelayPaths(
            slow=Values(min=2.0, avg=3.0, max=4.0),
            nominal=Values(min=1.0, avg=1.0, max=1.0),
        )
        result = a + b
        # slow is in both, so it's kept
        assert result.slow is not None
        assert result.slow.max == 7.0
        # fast and nominal are not in both
        assert result.fast is None
        assert result.nominal is None

    def test_compose_path_with_mixed_fields_scalar_is_none(self) -> None:
        """A path mixing slow-only and nominal-only edges yields None scalar."""
        sdf = (
            SDFBuilder()
            .set_header(timescale="1ps")
            .add_cell("BUF", "b1")
            .add_iopath("A", "Y", {"slow": {"min": 1.0, "avg": 2.0, "max": 3.0}})
            .add_cell("BUF", "b2")
            .add_iopath("A", "Y", {"nominal": {"min": 1.0, "avg": 2.0, "max": 3.0}})
            .add_interconnect(
                "b1/Y", "b2/A", {"slow": {"min": 0.5, "avg": 0.5, "max": 0.5}}
            )
            .build()
        )
        g = TimingGraph(sdf)
        paths = g.find_paths("b1/A", "b2/Y")
        assert len(paths) == 1
        delay = g.compose_delay(paths[0])
        # b2's IOPATH has nominal only, so slow disappears in composition
        assert delay.get_scalar("slow", "max") is None


class TestSourceEqualsSink:
    """Bug #4: source == sink should return empty paths, not crash."""

    def test_find_paths_source_equals_sink(self, spec1_graph: TimingGraph) -> None:
        """find_paths with source == sink should return empty list."""
        paths = spec1_graph.find_paths("P1/z", "P1/z")
        assert paths == []

    def test_missing_source_node(self, spec1_graph: TimingGraph) -> None:
        """find_paths with a nonexistent source should raise NodeNotFound."""
        with pytest.raises(nx.NodeNotFound):
            spec1_graph.find_paths("NONEXISTENT", "P2/i")

    def test_missing_sink_node(self, spec1_graph: TimingGraph) -> None:
        """find_paths with a nonexistent sink should raise NodeNotFound."""
        with pytest.raises(nx.NodeNotFound):
            spec1_graph.find_paths("P1/z", "NONEXISTENT")

    def test_source_equals_sink_nonexistent(self, spec1_graph: TimingGraph) -> None:
        """find_paths with source==sink on nonexistent node should raise."""
        with pytest.raises(nx.NodeNotFound):
            spec1_graph.find_paths("NONEXISTENT", "NONEXISTENT")


def _register_sdf() -> SDFFile:
    """One flop between two top-level pins: din -> ff/D, ff/Q -> dout."""
    return (
        SDFBuilder()
        .set_header(timescale="1ns")
        .add_cell("DFF", "ff")
        .add_iopath("CLK", "Q", {"nominal": {"min": 0.3, "max": 0.3}})
        .add_interconnect("din", "ff/D", {"nominal": {"min": 0.1, "max": 0.1}})
        .add_interconnect("ff/Q", "dout", {"nominal": {"min": 0.2, "max": 0.2}})
        .add_setup("CLK", "D", {"nominal": {"min": 0.05, "max": 0.05}})
        .add_hold("CLK", "D", {"nominal": {"min": -0.02, "max": -0.02}})
        .build()
    )


class TestTraverseRegisters:
    """Optional data-to-clock edges for setup-style timing checks."""

    def test_default_graph_stops_at_register(self) -> None:
        """Without the option, registers end timing paths."""
        g = TimingGraph(_register_sdf())
        assert "dout" not in nx.descendants(g.graph, "din")
        assert not g.graph.has_edge("ff/D", "ff/CLK")

    def test_register_becomes_traversable(self) -> None:
        """The data-to-clock edge lets paths continue through the flop."""
        g = TimingGraph(_register_sdf(), traverse_registers=True)
        assert "dout" in nx.descendants(g.graph, "din")

    def test_edge_carries_setup_time(self) -> None:
        """The pass-through edge's delay is the setup time."""
        g = TimingGraph(_register_sdf(), traverse_registers=True)
        arcs = g.graph.get_edge_data("ff/D", "ff/CLK")
        assert len(arcs) == 1
        (arc,) = arcs.values()
        assert arc["entry_type"] == EntryType.SETUP
        assert arc["delay"].get_scalar("nominal", "max") == 0.05

    def test_composed_path_includes_setup_and_clk_to_q(self) -> None:
        """din -> dout composes interconnects, setup, and CLK->Q IOPATH."""
        g = TimingGraph(_register_sdf(), traverse_registers=True)
        paths = g.find_paths("din", "dout")
        assert len(paths) == 1
        delay = g.compose_delay(paths[0])
        # 0.1 (din->D) + 0.05 (setup) + 0.3 (CLK->Q) + 0.2 (Q->dout)
        assert delay.get_scalar("nominal", "max") == pytest.approx(0.65)

    def test_hold_check_adds_no_edge(self) -> None:
        """HOLD is a minimum-arrival constraint, not a propagation cost."""
        sdf = (
            SDFBuilder()
            .set_header(timescale="1ns")
            .add_cell("DFF", "ff")
            .add_hold("CLK", "D", {"nominal": {"min": -0.02, "max": -0.02}})
            .build()
        )
        g = TimingGraph(sdf, traverse_registers=True)
        assert not g.graph.has_edge("ff/D", "ff/CLK")

    def test_setuphold_uses_setup_half(self) -> None:
        """SETUPHOLD contributes its setup value, never the hold value."""
        sdf = (
            SDFBuilder()
            .set_header(timescale="1ns")
            .add_cell("DFF", "ff")
            .add_setuphold(
                "CLK",
                "D",
                {
                    "setup": {"min": 0.07, "max": 0.07},
                    "hold": {"min": -0.03, "max": -0.03},
                },
            )
            .build()
        )
        g = TimingGraph(sdf, traverse_registers=True)
        arcs = g.graph.get_edge_data("ff/D", "ff/CLK")
        assert len(arcs) == 1
        (arc,) = arcs.values()
        assert arc["entry_type"] == EntryType.SETUPHOLD
        assert arc["delay"].get_scalar("nominal", "max") == 0.07
        assert arc["delay"].get_scalar("hold", "max") is None

    def test_parsed_setup_variants_become_parallel_edges(self) -> None:
        """Edge-specific SETUP variants each add their own parallel edge."""
        sdf_text = """(DELAYFILE
         (SDFVERSION "3.0")
         (DESIGN "top")
         (DIVIDER /)
         (TIMESCALE 1ns)
         (CELL
          (CELLTYPE "DFF")
          (INSTANCE ff)
          (TIMINGCHECK
           (SETUP (posedge D) (posedge CLK) (0.11::0.11))
           (SETUP (negedge D) (posedge CLK) (0.13::0.13))
          )
         )
        )"""
        from sdf_toolkit.io import parse

        g = TimingGraph(parse(sdf_text), traverse_registers=True)
        arcs = g.graph.get_edge_data("ff/D", "ff/CLK")
        assert len(arcs) == 2
        scalars = sorted(
            arc["delay"].get_scalar("nominal", "max") for arc in arcs.values()
        )
        assert scalars == [0.11, 0.13]
