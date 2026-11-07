"""Structural queries on a TimingGraph: views, nearest targets and convergence."""

import json
import subprocess
import sys
import textwrap

import networkx as nx
import pytest

from sdf_toolkit.core.identifiers import SDFNameError
from sdf_toolkit.core.pathgraph import TimingGraph, TimingGraphError
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
