"""Structural queries on a TimingGraph: views, nearest targets and convergence."""

import json
import subprocess
import sys
import textwrap

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
    print(json.dumps({"within": list(view.graph)}))
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
    assert json.loads(result.stdout) == {"within": pins}
