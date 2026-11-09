from decimal import Decimal

import pytest

from sdf_toolkit.core.enums import Corner, EdgeSelect
from sdf_toolkit.core.pathgraph import (
    RankedPath,
    TimingGraph,
    compute_slack,
    critical_path,
    rank_paths,
)
from sdf_toolkit.core.sdffile import SDFFile, SDFHeader
from sdf_toolkit.core.values import DelayValues, MissingDelayError, Values

_RISE_FALL = DelayValues.of(
    Values(min=1.0, typ=2.0, max=3.0), Values(min=0.5, typ=4.0, max=2.5)
)


class TestScalar:
    @pytest.mark.parametrize(
        ("corner", "edge", "expected"),
        [
            (Corner.MAX, EdgeSelect.WORST, 3.0),
            (Corner.MAX, EdgeSelect.BEST, 2.5),
            (Corner.MIN, EdgeSelect.RISE, 1.0),
            (Corner.MIN, EdgeSelect.FALL, 0.5),
            (Corner.TYP, EdgeSelect.WORST, 4.0),
        ],
    )
    def test_corner_and_edge(self, corner, edge, expected) -> None:
        assert _RISE_FALL.scalar(corner=corner, edge=edge) == expected

    def test_defaults_are_max_worst(self) -> None:
        assert _RISE_FALL.scalar() == 3.0

    def test_absent_corner_raises(self) -> None:
        delay = DelayValues.of(Values(min=1.0, typ=None, max=3.0))
        with pytest.raises(MissingDelayError):
            delay.scalar(corner=Corner.TYP)


class TestStartpointsEndpoints:
    def test_startpoints(self, spec1_graph: TimingGraph) -> None:
        starts = spec1_graph.startpoints()
        assert "P1/z" in starts
        # All startpoints have no incoming edges
        for node in starts:
            assert spec1_graph.predecessors(node) == []

    def test_endpoints(self, spec1_graph: TimingGraph) -> None:
        ends = spec1_graph.endpoints()
        assert "P2/i" in ends
        assert "P3/i" in ends

    def test_startpoints_not_in_endpoints(self, spec1_graph: TimingGraph) -> None:
        starts = spec1_graph.startpoints()
        ends = spec1_graph.endpoints()
        assert starts.isdisjoint(ends)

    def test_empty_graph(self) -> None:
        sdf = SDFFile(header=SDFHeader(), cells={})
        graph = TimingGraph(sdf)
        assert graph.startpoints() == set()
        assert graph.endpoints() == set()


class TestRankPaths:
    def test_rank_paths_descending(self, spec1_graph: TimingGraph) -> None:
        ranked = rank_paths(spec1_graph, "P1/z", "P2/i", corner=Corner.MAX)
        assert len(ranked) == 2
        assert isinstance(ranked[0], RankedPath)
        assert ranked[0].scalar is not None
        assert ranked[1].scalar is not None
        assert ranked[0].scalar >= ranked[1].scalar

    def test_rank_paths_ascending(self, spec1_graph: TimingGraph) -> None:
        ranked = rank_paths(
            spec1_graph, "P1/z", "P2/i", corner=Corner.MAX, descending=False
        )
        assert len(ranked) == 2
        assert ranked[0].scalar is not None
        assert ranked[1].scalar is not None
        assert ranked[0].scalar <= ranked[1].scalar

    def test_rank_paths_no_path(self, spec1_graph: TimingGraph) -> None:
        ranked = rank_paths(spec1_graph, "P2/i", "P1/z")
        assert ranked == []

    def test_rank_paths_min_corner(self, spec1_graph: TimingGraph) -> None:
        ranked = rank_paths(spec1_graph, "P1/z", "P2/i", corner=Corner.MIN)
        assert len(ranked) == 2
        for rp in ranked:
            assert rp.scalar is not None


class TestCriticalPath:
    def test_critical_path(self, spec2_graph: TimingGraph) -> None:
        cp = critical_path(spec2_graph, "top.ff1.CP", "top.ff1.QN")
        assert cp is not None
        assert cp.scalar is not None
        assert len(cp.edges) > 0

    def test_critical_path_is_slowest(self, spec2_graph: TimingGraph) -> None:
        cp = critical_path(spec2_graph, "top.ff1.CP", "top.ff1.QN", corner=Corner.MAX)
        ranked = rank_paths(spec2_graph, "top.ff1.CP", "top.ff1.QN", corner=Corner.MAX)
        assert cp is not None
        assert cp.scalar == ranked[0].scalar

    def test_critical_path_no_path(self, spec1_graph: TimingGraph) -> None:
        cp = critical_path(spec1_graph, "P2/i", "P1/z")
        assert cp is None


class TestComputeSlack:
    def test_positive_slack(self, spec2_graph: TimingGraph) -> None:
        result = compute_slack(
            spec2_graph, "top.ff1.CP", "top.ff1.QN", period=Decimal(10)
        )
        assert result is not None
        assert result > 0

    def test_negative_slack(self, spec2_graph: TimingGraph) -> None:
        result = compute_slack(
            spec2_graph, "top.ff1.CP", "top.ff1.QN", period=Decimal("0.5")
        )
        assert result is not None
        assert result < 0

    def test_slack_value(self, spec2_graph: TimingGraph) -> None:
        cp = critical_path(spec2_graph, "top.ff1.CP", "top.ff1.QN", corner=Corner.MAX)
        result = compute_slack(
            spec2_graph,
            "top.ff1.CP",
            "top.ff1.QN",
            period=Decimal(10),
            corner=Corner.MAX,
        )
        assert cp is not None
        assert cp.scalar is not None
        assert result is not None
        assert result == 10 - cp.scalar

    def test_slack_no_path(self, spec1_graph: TimingGraph) -> None:
        result = compute_slack(spec1_graph, "P2/i", "P1/z", period=Decimal(10))
        assert result is None
