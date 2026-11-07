"""Structural queries on a TimingGraph: views, nearest targets and convergence."""

import json
import random
import subprocess
import sys
import textwrap
from collections.abc import Callable
from decimal import Decimal

import networkx as nx
import pytest

from sdf_toolkit.core.identifiers import SDFNameError
from sdf_toolkit.core.model import Corner, EdgeSelect
from sdf_toolkit.core.pathgraph import (
    RankedPath,
    TimingEdge,
    TimingGraph,
    TimingGraphError,
    TimingLoopError,
    critical_path,
    fastest_path,
)
from sdf_toolkit.parser.parser import parse_sdf

# A-B-D-E is the fewest-hop path from A to E and A-C-D-E the lightest.  F-G-H
# is not connected to it.
SHARED_EDGES = (
    ("A", "B", "1"),
    ("A", "C", "2"),
    ("B", "D", "3"),
    ("C", "D", "1"),
    ("D", "E", "4"),
    ("F", "G", "1"),
    ("G", "H", "1"),
)


def _graph(edges: tuple[tuple[str, str, str], ...], *, cells: str = "") -> TimingGraph:
    """Build a graph of top-level INTERCONNECT arcs, each one ``(delay)``."""
    interconnects = " ".join(f"(INTERCONNECT {a} {b} ({d}))" for a, b, d in edges)
    return TimingGraph(
        parse_sdf(
            '(DELAYFILE (SDFVERSION "3.0") (DIVIDER /)'
            f' (CELL (CELLTYPE "TOP") (INSTANCE) (DELAY (ABSOLUTE {interconnects})))'
            f" {cells})"
        )
    )


def _arcs(graph: TimingGraph) -> list[tuple[str, str]]:
    return [(arc.source, arc.sink) for arc in graph.edges()]


@pytest.fixture
def shared() -> TimingGraph:
    return _graph(SHARED_EDGES)


class TestPrimaryPorts:
    def test_lists_top_level_sources_and_sinks(self, shared: TimingGraph) -> None:
        assert shared.primary_inputs() == ["A", "F"]
        assert shared.primary_outputs() == ["E", "H"]

    def test_hierarchical_pins_are_not_ports(self) -> None:
        graph = _graph((("u1/Y", "u2/A", "1"),))
        assert graph.primary_inputs() == []
        assert graph.primary_outputs() == []

    def test_an_escaped_divider_stays_in_a_top_level_name(self) -> None:
        graph = _graph(((r"a\/b", "u/A", "1"), ("u/A", r"c\/d", "1")))
        assert graph.primary_inputs() == [r"a\/b"]
        assert graph.primary_outputs() == [r"c\/d"]


class TestWithin:
    def test_keeps_only_pins_inside_the_instance(self) -> None:
        graph = _graph((("P/A", "P/B", "1"), ("P/B", "Q/A", "1"), ("Q/A", "Q/B", "1")))
        view = graph.within("P")
        assert _arcs(view) == [("P/A", "P/B")]
        assert view.nodes() == {"P/A", "P/B"}

    def test_leaves_the_original_unchanged(self) -> None:
        graph = _graph((("P/A", "P/B", "1"), ("P/B", "Q/A", "1")))
        graph.within("P")
        assert _arcs(graph) == [("P/A", "P/B"), ("P/B", "Q/A")]

    @pytest.mark.parametrize(
        ("instance", "kept"),
        [
            pytest.param("u0", {"u0/a/Y", "u0/b/A"}, id="not-a-sibling-prefix"),
            pytest.param("u0/a", {"u0/a/Y"}, id="nested"),
            pytest.param("u01", {"u01/c/A"}, id="sibling"),
            pytest.param(r"x\/y", {r"x\/y/A"}, id="escaped-divider"),
        ],
    )
    def test_matches_whole_instance_names(self, instance: str, kept: set[str]) -> None:
        graph = _graph(
            (
                ("u0/a/Y", "u0/b/A", "1"),
                ("u0/b/A", "u01/c/A", "1"),
                (r"x\/y/A", "x/A", "1"),
            )
        )
        assert graph.within(instance).nodes() == kept

    def test_keeps_the_pin_and_arc_order_of_the_graph(self) -> None:
        names = [f"u/p{i}" for i in (7, 3, 9, 1, 8, 2, 6, 0, 5, 4)]
        edges = tuple((a, b, "1") for a, b in zip(names, names[1:], strict=False))
        graph = _graph((("in", names[0], "1"), *edges))
        view = graph.within("u")
        assert list(view.graph) == names
        assert _arcs(view) == [(a, b) for a, b, _ in edges]

    def test_restricts_start_and_end_points(self) -> None:
        graph = _graph((("in", "u/A", "1"), ("u/A", "u/B", "1"), ("u/B", "out", "1")))
        view = graph.within("u")
        assert view.startpoints() == {"u/A"}
        assert view.endpoints() == {"u/B"}

    @pytest.mark.parametrize("instance", ["", "P/", "P/Q/"])
    def test_rejects_an_empty_or_trailing_divider_path(self, instance: str) -> None:
        graph = _graph((("P/A", "P/B", "1"),))
        with pytest.raises(TimingGraphError, match="without a trailing divider"):
            graph.within(instance)

    def test_rejects_a_dangling_escape(self) -> None:
        with pytest.raises(SDFNameError):
            _graph((("P/A", "P/B", "1"),)).within("P\\")


class TestNearestPath:
    @pytest.mark.parametrize(
        ("source", "targets", "reverse", "expected"),
        [
            pytest.param("A", ["D", "E"], False, ["A", "B", "D"], id="forward"),
            pytest.param("E", ["A", "B"], True, ["E", "D", "B"], id="reverse"),
            pytest.param("A", ["E"], False, ["A", "B", "D", "E"], id="far"),
            pytest.param("D", ["D", "E"], False, ["D"], id="source-is-a-target"),
            pytest.param("A", ["H"], False, None, id="unreachable"),
            pytest.param("A", ["F"], True, None, id="unreachable-reverse"),
        ],
    )
    def test_finds_the_fewest_arc_path(
        self,
        shared: TimingGraph,
        source: str,
        targets: list[str],
        reverse: bool,
        expected: list[str] | None,
    ) -> None:
        assert shared.nearest_path(source, targets, reverse=reverse) == expected

    @pytest.mark.parametrize(
        ("edges", "expected"),
        [
            pytest.param((("A", "Q", "9"), ("A", "P", "1")), "Q", id="Q-first"),
            pytest.param((("A", "P", "9"), ("A", "Q", "1")), "P", id="P-first"),
        ],
    )
    def test_ties_go_to_the_earliest_arc(
        self, edges: tuple[tuple[str, str, str], ...], expected: str
    ) -> None:
        assert _graph(edges).nearest_path("A", ["P", "Q"]) == ["A", expected]

    @pytest.mark.parametrize(
        ("source", "target", "reverse"),
        [("u/A", "u/B", False), ("u/B", "u/A", True)],
        ids=["forward", "reverse"],
    )
    def test_a_within_view_never_leaves_the_instance(
        self, source: str, target: str, reverse: bool
    ) -> None:
        """The shorter route through o/X lies outside u in both directions."""
        graph = _graph(
            (
                ("u/A", "o/X", "1"),
                ("o/X", "u/B", "1"),
                ("u/A", "u/C", "1"),
                ("u/C", "u/D", "1"),
                ("u/D", "u/B", "1"),
            )
        )
        inside = ["u/A", "u/C", "u/D", "u/B"]
        expected = inside[::-1] if reverse else inside
        view = graph.within("u")
        assert view.nearest_path(source, [target], reverse=reverse) == expected
        unrestricted = graph.nearest_path(source, [target], reverse=reverse)
        assert unrestricted is not None
        assert "o/X" in unrestricted

    def test_rejects_empty_targets(self, shared: TimingGraph) -> None:
        with pytest.raises(TimingGraphError, match="at least one target"):
            shared.nearest_path("A", [])

    @pytest.mark.parametrize(
        ("source", "targets"), [("NOPE", ["A"]), ("A", ["D", "NOPE"])]
    )
    def test_rejects_unknown_pins(
        self, shared: TimingGraph, source: str, targets: list[str]
    ) -> None:
        with pytest.raises(nx.NodeNotFound, match="NOPE"):
            shared.nearest_path(source, targets)


class TestConvergencePin:
    @pytest.mark.parametrize(
        ("sources", "expected"),
        [
            pytest.param(["B", "C"], "D", id="two-sources"),
            pytest.param(["C", "B", "C"], "D", id="order-and-repeats"),
            pytest.param(["A", "D"], "D", id="source-on-the-other-path"),
            pytest.param(["A"], "A", id="single-source"),
            pytest.param(["A", "F"], None, id="disjoint"),
        ],
    )
    def test_shared_graph(
        self, shared: TimingGraph, sources: list[str], expected: str | None
    ) -> None:
        assert shared.convergence_pin(sources) == expected

    @pytest.mark.parametrize(
        ("edges", "expected"),
        [
            pytest.param(
                (
                    ("S1", "A", "1"),
                    ("S2", "B", "1"),
                    ("A", "X", "1"),
                    ("B", "X", "1"),
                    ("A", "Y", "1"),
                    ("B", "Y", "2"),
                ),
                "X",
                id="by-hops",
            ),
            pytest.param(
                (
                    ("S1", "A", "1"),
                    ("S2", "A", "1"),
                    ("S1", "B", "1"),
                    ("S2", "B", "1"),
                    ("A", "C", "1"),
                    ("C", "D", "1"),
                ),
                "A",
                id="by-reach",
            ),
            pytest.param(
                (
                    ("S1", "B", "1"),
                    ("S2", "B", "1"),
                    ("S1", "A", "1"),
                    ("S2", "A", "1"),
                ),
                "A",
                id="by-pin-name",
            ),
            pytest.param(
                (
                    ("S1", "L2", "1"),
                    ("S2", "L1", "1"),
                    ("L1", "L2", "1"),
                    ("L2", "L1", "1"),
                    ("L1", "OUT", "1"),
                ),
                "L1",
                id="loop-is-one-region",
            ),
        ],
    )
    def test_tie_breaks(
        self, edges: tuple[tuple[str, str, str], ...], expected: str
    ) -> None:
        assert _graph(edges).convergence_pin(["S1", "S2"]) == expected

    def test_a_loop_through_one_source_still_starts_at_it(self) -> None:
        graph = _graph((("A", "B", "1"), ("B", "A", "1"), ("B", "C", "1")))
        assert graph.convergence_pin(["A"]) == "A"

    def test_rejects_empty_sources(self, shared: TimingGraph) -> None:
        with pytest.raises(TimingGraphError, match="at least one source"):
            shared.convergence_pin([])

    def test_rejects_unknown_pins(self, shared: TimingGraph) -> None:
        with pytest.raises(nx.NodeNotFound, match="NOPE"):
            shared.convergence_pin(["A", "NOPE"])


_SEED_PROBE = textwrap.dedent(
    """
    import json
    from sdf_toolkit.core.pathgraph import TimingGraph
    from sdf_toolkit.parser.parser import parse_sdf

    pins = [f"u/p{i:02}" for i in range(40)][::-1]
    arcs = " ".join(
        f"(INTERCONNECT in {p} (1)) (INTERCONNECT {p} out (1))" for p in pins
    )
    # networkx orders a subgraph by its pin set once it keeps under half the
    # graph, which is the case the probe must cover.
    arcs += " ".join(f" (INTERCONNECT in x/q{i} (1))" for i in range(100))
    graph = TimingGraph(parse_sdf(
        '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "T") (INSTANCE)'
        f" (DELAY (ABSOLUTE {arcs}))))"
    ))
    view = graph.within("u")
    print(json.dumps({
        "within": list(view.graph),
        "nearest": graph.nearest_path("in", pins[::7]),
        "reverse": graph.nearest_path("out", pins[3:], reverse=True),
        "convergence": graph.convergence_pin(pins[::5]),
    }))
    """
)


@pytest.mark.parametrize("seed", range(6))
def test_results_do_not_depend_on_the_hash_seed(seed: int) -> None:
    """Ties resolve by graph order, never by the order of a Python set."""
    result = subprocess.run(
        [sys.executable, "-c", _SEED_PROBE],
        env={"PYTHONHASHSEED": str(seed)},
        capture_output=True,
        text=True,
        check=True,
    )
    pins = [f"u/p{i:02}" for i in range(40)][::-1]
    assert json.loads(result.stdout) == {
        "within": pins,
        "nearest": ["in", pins[0]],
        "reverse": ["out", pins[3]],
        "convergence": "out",
    }


def _rise_fall_graph(
    edges: tuple[tuple[str, str, str, str], ...],
) -> TimingGraph:
    """Build a graph of top-level arcs, each with a ``(rise) (fall)`` delay."""
    return _graph(tuple((a, b, f"{r}) ({f}") for a, b, r, f in edges))


def _max_weight(arc: TimingEdge) -> Decimal:
    return arc.delay.scalar(corner=Corner.MAX)


class TestFastestPath:
    def test_takes_the_lightest_route(self, shared: TimingGraph) -> None:
        fast = fastest_path(shared, "A", "E")
        slow = critical_path(shared, "A", "E")
        assert fast is not None
        assert slow is not None
        assert fast.scalar == Decimal(7)
        assert [arc.sink for arc in fast.edges] == ["C", "D", "E"]
        assert slow.scalar == Decimal(8)

    @pytest.mark.parametrize(
        ("edge", "expected", "via"),
        [
            pytest.param(EdgeSelect.RISE, "1", "P", id="rise"),
            pytest.param(EdgeSelect.FALL, "5", "Q", id="fall"),
            pytest.param(EdgeSelect.BEST, "1", "P", id="best"),
        ],
    )
    def test_composes_each_transition_along_the_path(
        self, edge: EdgeSelect, expected: str, via: str
    ) -> None:
        graph = _rise_fall_graph(
            (("A", "P", "0.5", "9"), ("P", "Z", "0.5", "1"), ("A", "Q", "2", "2"))
            + (("Q", "Z", "3", "3"),)
        )
        path = fastest_path(graph, "A", "Z", corner=Corner.MAX, edge=edge)
        assert path is not None
        assert path.scalar == Decimal(expected)
        assert path.edges[0].sink == via

    def test_rejects_worst(self, shared: TimingGraph) -> None:
        with pytest.raises(TimingGraphError, match="no shortest-path form"):
            fastest_path(shared, "A", "E", edge=EdgeSelect.WORST)

    @pytest.mark.parametrize(("source", "sink"), [("A", "A"), ("A", "H"), ("E", "A")])
    def test_no_path_gives_none(
        self, shared: TimingGraph, source: str, sink: str
    ) -> None:
        assert fastest_path(shared, source, sink) is None

    def test_a_loop_on_the_way_raises(self) -> None:
        graph = _graph((("A", "B", "1"), ("B", "C", "1"), ("C", "B", "1")))
        with pytest.raises(TimingLoopError):
            fastest_path(graph, "A", "C")

    @pytest.mark.parametrize("seed", range(8))
    @pytest.mark.parametrize(
        "edge", [EdgeSelect.RISE, EdgeSelect.FALL, EdgeSelect.BEST]
    )
    def test_agrees_with_enumeration(self, seed: int, edge: EdgeSelect) -> None:
        """On a random acyclic graph the fastest path is the minimum of all."""
        rng = random.Random(seed)
        pins = [f"n{i}" for i in range(9)]
        edges = tuple(
            (a, b, str(rng.randint(0, 9)), str(rng.randint(0, 9)))
            for i, a in enumerate(pins)
            for b in pins[i + 1 :]
            if rng.random() < 0.4
        )
        graph = _rise_fall_graph((("n0", "n8", "50", "50"), *edges))
        delays = [
            graph.compose_delay(path).scalar(corner=Corner.MAX, edge=edge)
            for path in graph.find_paths("n0", "n8")
        ]
        fast = fastest_path(graph, "n0", "n8", corner=Corner.MAX, edge=edge)
        assert fast is not None
        assert fast.scalar == min(delays)
        assert fast.delay.scalar(corner=Corner.MAX, edge=edge) == fast.scalar


class TestThrough:
    @pytest.fixture
    def fork(self) -> TimingGraph:
        return _graph(
            (
                ("in", "a", "1"),
                ("a", "out", "1"),
                ("in", "b", "5"),
                ("b", "out", "5"),
            )
        )

    @pytest.mark.parametrize(
        ("analysis", "through", "expected"),
        [
            pytest.param(critical_path, "a", "2", id="critical-through-a"),
            pytest.param(critical_path, None, "10", id="critical-any"),
            pytest.param(fastest_path, "b", "10", id="fastest-through-b"),
            pytest.param(fastest_path, None, "2", id="fastest-any"),
            pytest.param(critical_path, "in", "10", id="through-the-source"),
            pytest.param(critical_path, "out", "10", id="through-the-sink"),
        ],
    )
    def test_keeps_paths_through_the_pin(
        self,
        fork: TimingGraph,
        analysis: Callable[..., RankedPath | None],
        through: str | None,
        expected: str,
    ) -> None:
        path = analysis(fork, "in", "out", corner=Corner.MAX, through=through)
        assert path is not None
        assert path.scalar == Decimal(expected)
        assert path.edges[0].source == "in"
        assert path.edges[-1].sink == "out"
        assert path.delay == fork.compose_delay(path.edges)

    def test_joins_the_same_transition_on_both_legs(self) -> None:
        """Rise on one leg never pairs with fall on the other."""
        graph = _rise_fall_graph((("in", "m", "1", "9"), ("m", "out", "9", "1")))
        path = critical_path(graph, "in", "out", through="m")
        assert path is not None
        assert path.scalar == Decimal(10)

    def test_an_unreached_pin_gives_none(self, fork: TimingGraph) -> None:
        graph = _graph((*SHARED_EDGES, ("X", "Y", "1")))
        assert critical_path(graph, "A", "E", through="X") is None
        assert critical_path(fork, "a", "out", through="b") is None

    def test_rejects_an_unknown_pin(self, fork: TimingGraph) -> None:
        with pytest.raises(nx.NodeNotFound, match="NOPE"):
            critical_path(fork, "in", "out", through="NOPE")


class TestLightestPath:
    @pytest.mark.parametrize(
        ("sink", "expected", "via"),
        [("E", "7", ["C", "D", "E"]), ("D", "3", ["C", "D"]), ("A", "0", [])],
        ids=["to-sink", "fewer-arcs-heavier", "source-is-sink"],
    )
    def test_sums_the_weights(
        self, shared: TimingGraph, sink: str, expected: str, via: list[str]
    ) -> None:
        found = shared.lightest_path("A", sink, _max_weight)
        assert found is not None
        total, arcs = found
        assert total == Decimal(expected)
        assert [arc.sink for arc in arcs] == via

    def test_sums_each_arcs_own_worst_transition(self) -> None:
        graph = _rise_fall_graph((("A", "B", "1", "4"), ("B", "C", "4", "1")))
        found = graph.lightest_path("A", "C", _max_weight)
        assert found is not None
        assert found[0] == Decimal(8)
        cp = critical_path(graph, "A", "C")
        assert cp is not None
        assert cp.scalar == Decimal(5)

    def test_a_none_weight_leaves_the_arc_out(self, shared: TimingGraph) -> None:
        def weight(arc: TimingEdge) -> Decimal | None:
            return None if arc.source == "C" else _max_weight(arc)

        found = shared.lightest_path("A", "E", weight)
        assert found is not None
        assert found[0] == Decimal(8)
        assert shared.lightest_path("C", "E", weight) is None

    def test_takes_the_lightest_parallel_arc(self) -> None:
        graph = _graph(
            (("in", "u/A", "0"),),
            cells='(CELL (CELLTYPE "BUF") (INSTANCE u) (DELAY (ABSOLUTE'
            " (IOPATH (posedge A) Y (3)) (IOPATH (negedge A) Y (1)))))",
        )
        found = graph.lightest_path("u/A", "u/Y", _max_weight)
        assert found is not None
        total, (arc,) = found
        assert total == Decimal(1)
        assert arc.delay.scalar() == Decimal(1)

    @pytest.mark.parametrize(("source", "sink"), [("A", "H"), ("E", "A")])
    def test_no_path_gives_none(
        self, shared: TimingGraph, source: str, sink: str
    ) -> None:
        assert shared.lightest_path(source, sink, _max_weight) is None

    def test_rejects_a_negative_weight(self, shared: TimingGraph) -> None:
        with pytest.raises(TimingGraphError, match="non-negative"):
            shared.lightest_path("A", "E", lambda arc: -_max_weight(arc))
