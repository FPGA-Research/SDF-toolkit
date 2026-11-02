import networkx as nx
import pytest

from sdf_toolkit.core.builder import SDFBuilder
from sdf_toolkit.core.model import (
    Corner,
    DelayValues,
    Divider,
    EdgeSelect,
    EntryType,
    MissingDelayError,
    PortSpec,
    SDFFile,
    SDFHeader,
    Timescale,
    TimingPortSpec,
    Values,
)
from sdf_toolkit.core.pathgraph import (
    ARC_ATTR,
    TimingGraph,
    VerificationResult,
    batch_endpoint_analysis,
    critical_path,
    decompose_delay,
    rank_paths,
    verify_path,
)


def _min_max(*pairs: tuple[float, float]) -> DelayValues:
    """A delay whose rvalues are ``(min::max)`` triples, as the fixtures write."""
    return DelayValues(rvalues=tuple(Values(min=lo, max=hi) for lo, hi in pairs))


def _triple(min_: float | None, typ: float | None, max_: float | None) -> DelayValues:
    return DelayValues.of(Values(min=min_, typ=typ, max=max_))


def _builder() -> SDFBuilder:
    """A builder whose names use ``/`` between instance and pin, as the tests do."""
    return SDFBuilder().set_header(
        timescale=Timescale.parse("1ps"), divider=Divider.SLASH
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

        # Two rvalues are the rise and fall delays.
        expected_path1 = _min_max((1.805, 1.805), (1.795, 1.795))
        expected_path2 = _min_max((1.355, 1.355), (1.380, 1.380))

        assert any(expected_path1.approx_eq(d, tolerance=1e-6) for d in delays)
        assert any(expected_path2.approx_eq(d, tolerance=1e-6) for d in delays)

    def test_compose_empty_path(self, spec1_graph: TimingGraph) -> None:
        with pytest.raises(ValueError, match="empty path"):
            spec1_graph.compose_delay([])


class TestVerifyPath:
    def test_verify_pass(self, spec1_graph: TimingGraph) -> None:
        expected = _min_max((1.805, 1.805), (1.795, 1.795))
        result = verify_path(
            spec1_graph, "P1/z", "P2/i", expected=expected, tolerance=1e-6
        )
        assert isinstance(result, VerificationResult)
        assert result.passed is True
        assert result.source == "P1/z"
        assert result.sink == "P2/i"

    def test_verify_fail(self, spec1_graph: TimingGraph) -> None:
        expected = _min_max((999.0, 999.0), (999.0, 999.0))
        result = verify_path(spec1_graph, "P1/z", "P2/i", expected=expected)
        assert result.passed is False


class TestDecomposeDelay:
    def test_decompose(self) -> None:
        total = _min_max((1.805, 1.805), (1.795, 1.795))
        known = _min_max((1.0, 1.0), (1.0, 1.0))
        unknown = decompose_delay(total, known)
        expected = _min_max((0.805, 0.805), (0.795, 0.795))
        assert unknown.approx_eq(expected, tolerance=1e-9)


class TestGraphSkipsNonRoutableEntries:
    """Only IOPATH, INTERCONNECT and setup checks become arcs."""

    def test_setup_hold_entries_skipped(self) -> None:
        limit = Values.uniform(0.5)
        sdf = (
            _builder()
            .add_cell("FF", "ff0")
            .add_setup(
                data=TimingPortSpec("D"), reference=TimingPortSpec("CLK"), limit=limit
            )
            .add_hold(
                data=TimingPortSpec("D"), reference=TimingPortSpec("CLK"), limit=limit
            )
            .add_iopath(
                input=PortSpec("D"), output=PortSpec("Q"), delay=_triple(1.0, 1.0, 1.0)
            )
            .build()
        )
        graph = TimingGraph(sdf, traverse_registers=False)
        # Only the IOPATH should create edges; SETUP/HOLD are skipped
        assert len(graph.edges()) == 1

    def test_undriven_port_and_path_constraint_add_no_arc(self) -> None:
        sdf = (
            _builder()
            .add_cell("A", "a0")
            .add_port(port=PortSpec("A"), delay=_triple(1.0, 1.0, 1.0))
            .add_path_constraint(
                ports=("a0/A", "a0/Y"),
                rise=Values.uniform(1.0),
                fall=Values.uniform(1.0),
            )
            .build()
        )
        assert TimingGraph(sdf).edges() == []


class TestRankPathsNoneScalar:
    def test_rank_paths_with_absent_corner(self, spec1_graph: TimingGraph) -> None:
        """A corner the file leaves empty yields None scalars that still sort."""
        ranked = rank_paths(spec1_graph, "P1/z", "P2/i", corner=Corner.TYP)
        # spec-example1 writes (min::max), so typ is absent
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
            _builder()
            .add_cell("BUF", "b0")
            .add_iopath(
                input=PortSpec("A"), output=PortSpec("Y"), delay=_triple(1.0, 2.0, 3.0)
            )
            .add_iopath(
                input=PortSpec("A"), output=PortSpec("Y"), delay=_triple(4.0, 5.0, 6.0)
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

    def test_find_paths_no_duplicate_delays(self, parallel_graph: TimingGraph) -> None:
        """Each parallel edge path should have a distinct delay."""
        paths = parallel_graph.find_paths("b0/A", "b0/Y")
        delays = [parallel_graph.compose_delay(p) for p in paths]
        scalars = sorted(d.scalar(corner=Corner.MAX) for d in delays)
        assert scalars == [3.0, 6.0]

    def test_rank_paths_count_with_parallel_edges(
        self, parallel_graph: TimingGraph
    ) -> None:
        ranked = rank_paths(parallel_graph, "b0/A", "b0/Y", corner=Corner.MAX)
        assert len(ranked) == 2

    def test_critical_path_with_parallel_edges(
        self, parallel_graph: TimingGraph
    ) -> None:
        cp = critical_path(parallel_graph, "b0/A", "b0/Y", corner=Corner.MAX)
        assert cp is not None
        assert cp.scalar == 6.0

    def test_batch_endpoint_path_count_with_parallel_edges(
        self, parallel_graph: TimingGraph
    ) -> None:
        results = batch_endpoint_analysis(parallel_graph, corner=Corner.MAX)
        assert len(results) == 1
        assert results[0].path_count == 2


class TestParallelEdgesMultiHop:
    """Parallel edges on multi-hop paths."""

    @pytest.fixture
    def multi_hop_parallel_graph(self) -> TimingGraph:
        """a/Y --(2 edges)--> b/A -> b/Y with 1 edge."""
        sdf = (
            _builder()
            .add_cell("BUF", "b")
            .add_iopath(
                input=PortSpec("A"), output=PortSpec("Y"), delay=_triple(1.0, 1.0, 1.0)
            )
            .add_cell("TOP", "")
            .add_interconnect(
                source=PortSpec("a/Y"),
                load=PortSpec("b/A"),
                delay=_triple(2.0, 2.0, 2.0),
            )
            .add_interconnect(
                source=PortSpec("a/Y"),
                load=PortSpec("b/A"),
                delay=_triple(3.0, 3.0, 3.0),
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


def _link(builder: SDFBuilder, source: str, load: str, delay: DelayValues) -> None:
    builder.add_cell("TOP", "").add_interconnect(
        source=PortSpec(source), load=PortSpec(load), delay=delay
    )


class TestNoneScalarSorting:
    """Bug #3: None scalars should sort last in rank_paths, not first."""

    @pytest.fixture
    def missing_max_graph(self) -> TimingGraph:
        """Graph where the b2 branch gives no max delay, so its scalar is None."""
        builder = _builder()
        full, no_max = _triple(0.5, 0.5, 0.5), _triple(0.5, 0.5, None)
        builder.add_cell("BUF", "b1").add_iopath(
            input=PortSpec("A"), output=PortSpec("Y"), delay=_triple(1.0, 2.0, 3.0)
        )
        builder.add_cell("BUF", "b2").add_iopath(
            input=PortSpec("A"), output=PortSpec("Y"), delay=_triple(10.0, 20.0, None)
        )
        _link(builder, "src/Y", "b1/A", full)
        _link(builder, "src/Y", "b2/A", no_max)
        _link(builder, "b1/Y", "sink/A", full)
        _link(builder, "b2/Y", "sink/A", no_max)
        return TimingGraph(builder.build())

    def test_none_scalars_sort_last_descending(
        self, missing_max_graph: TimingGraph
    ) -> None:
        """When ranking by max, paths without a max go last."""
        ranked = rank_paths(
            missing_max_graph, "src/Y", "sink/A", corner=Corner.MAX, descending=True
        )
        assert len(ranked) == 2
        assert ranked[0].scalar is not None
        assert ranked[1].scalar is None

    def test_none_scalars_sort_last_ascending(
        self, missing_max_graph: TimingGraph
    ) -> None:
        """When ascending, None scalars still go last."""
        ranked = rank_paths(
            missing_max_graph, "src/Y", "sink/A", corner=Corner.MAX, descending=False
        )
        assert len(ranked) == 2
        assert ranked[-1].scalar is None

    def test_critical_path_skips_none_scalar(
        self, missing_max_graph: TimingGraph
    ) -> None:
        """critical_path should return a path with a real scalar, not None."""
        cp = critical_path(missing_max_graph, "src/Y", "sink/A", corner=Corner.MAX)
        assert cp is not None
        assert cp.scalar == pytest.approx(4.0, abs=1e-6)

    def test_other_corner_ranks_both(self, missing_max_graph: TimingGraph) -> None:
        """At the min corner both branches have values; b2 is the slower one."""
        cp = critical_path(missing_max_graph, "src/Y", "sink/A", corner=Corner.MIN)
        assert cp is not None
        assert cp.scalar == pytest.approx(11.0, abs=1e-6)


class TestTransitionComposition:
    """Composition adds transition by transition."""

    def test_rise_fall_path_composes_per_transition(self) -> None:
        builder = _builder()
        builder.add_cell("BUF", "b1").add_iopath(
            input=PortSpec("A"), output=PortSpec("Y"), delay=_min_max((1, 1), (2, 2))
        )
        _link(builder, "b1/Y", "sink/A", _min_max((0.5, 0.5)))
        g = TimingGraph(builder.build())
        (path,) = g.find_paths("b1/A", "sink/A")
        delay = g.compose_delay(path)
        assert delay == _min_max((1.5, 1.5), (2.5, 2.5))
        assert delay.scalar(corner=Corner.MAX, edge=EdgeSelect.RISE) == 1.5
        assert delay.scalar(corner=Corner.MAX, edge=EdgeSelect.FALL) == 2.5

    def test_missing_corner_on_one_edge_drops_it_from_the_path(self) -> None:
        """An edge without a max value leaves the composed path without one."""
        builder = _builder()
        builder.add_cell("BUF", "b1").add_iopath(
            input=PortSpec("A"), output=PortSpec("Y"), delay=_triple(1.0, 2.0, 3.0)
        )
        _link(builder, "b1/Y", "sink/A", _triple(0.5, 0.5, None))
        g = TimingGraph(builder.build())
        (path,) = g.find_paths("b1/A", "sink/A")
        delay = g.compose_delay(path)
        with pytest.raises(MissingDelayError):
            delay.scalar(corner=Corner.MAX)
        assert delay.scalar(corner=Corner.MIN) == 1.5


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


_D = TimingPortSpec("D")
_CLK = TimingPortSpec("CLK")


def _register_sdf() -> SDFFile:
    """One flop between two top-level pins: din -> ff/D, ff/Q -> dout."""
    builder = SDFBuilder().set_header(
        timescale=Timescale.parse("1ns"), divider=Divider.SLASH
    )
    (
        builder.add_cell("DFF", "ff")
        .add_iopath(
            input=PortSpec("CLK"), output=PortSpec("Q"), delay=_min_max((0.3, 0.3))
        )
        .add_setup(data=_D, reference=_CLK, limit=Values(min=0.05, max=0.05))
        .add_hold(data=_D, reference=_CLK, limit=Values(min=-0.02, max=-0.02))
    )
    _link(builder, "din", "ff/D", _min_max((0.1, 0.1)))
    _link(builder, "ff/Q", "dout", _min_max((0.2, 0.2)))
    return builder.build()


class TestTraverseRegisters:
    """Optional data-to-clock edges for setup-style timing checks."""

    def test_default_graph_stops_at_register(self) -> None:
        """Without the option, registers end timing paths."""
        g = TimingGraph(_register_sdf(), traverse_registers=False)
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
        assert arc[ARC_ATTR].entry_type == EntryType.SETUP
        assert arc[ARC_ATTR].delay.scalar(corner=Corner.MAX) == 0.05

    def test_composed_path_includes_setup_and_clk_to_q(self) -> None:
        """din -> dout composes interconnects, setup, and CLK->Q IOPATH."""
        g = TimingGraph(_register_sdf(), traverse_registers=True)
        paths = g.find_paths("din", "dout")
        assert len(paths) == 1
        delay = g.compose_delay(paths[0])
        # 0.1 (din->D) + 0.05 (setup) + 0.3 (CLK->Q) + 0.2 (Q->dout)
        assert delay.scalar(corner=Corner.MAX) == pytest.approx(0.65)

    def test_hold_check_adds_no_edge(self) -> None:
        """HOLD is a minimum-arrival constraint, not a propagation cost."""
        sdf = (
            _builder()
            .add_cell("DFF", "ff")
            .add_hold(data=_D, reference=_CLK, limit=Values(min=-0.02, max=-0.02))
            .build()
        )
        g = TimingGraph(sdf, traverse_registers=True)
        assert not g.graph.has_edge("ff/D", "ff/CLK")

    def test_setuphold_uses_setup_half(self) -> None:
        """SETUPHOLD contributes its setup value, never the hold value."""
        sdf = (
            _builder()
            .add_cell("DFF", "ff")
            .add_setuphold(
                data=_D,
                reference=_CLK,
                setup=Values(min=0.07, max=0.07),
                hold=Values(min=-0.03, max=-0.03),
            )
            .build()
        )
        g = TimingGraph(sdf, traverse_registers=True)
        arcs = g.graph.get_edge_data("ff/D", "ff/CLK")
        assert len(arcs) == 1
        (arc,) = arcs.values()
        assert arc[ARC_ATTR].entry_type == EntryType.SETUPHOLD
        assert arc[ARC_ATTR].delay == _min_max((0.07, 0.07))

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
            arc[ARC_ATTR].delay.scalar(corner=Corner.MAX) for arc in arcs.values()
        )
        assert scalars == [0.11, 0.13]
