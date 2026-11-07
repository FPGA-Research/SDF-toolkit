"""Kept search results never change what a search returns."""

import random

import networkx as nx
import pytest
from conftest import DATA_DIR

from sdf_toolkit.core import pathgraph
from sdf_toolkit.core.model import Corner, EdgeSelect
from sdf_toolkit.core.pathgraph import (
    TimingGraph,
    TimingLoopError,
    critical_path,
    fastest_path,
)
from sdf_toolkit.parser.parser import parse_sdf, parse_sdf_file

_INSTANCES = ("Inst_LA_FABULOUS_LC", "Inst_A_IOBUF")
_CORNERS = (
    (Corner.MAX, Corner.MIN),
    (Corner.TYP, Corner.TYP),
    (Corner.MIN, Corner.MAX),
)


@pytest.fixture(scope="module")
def sky130() -> TimingGraph:
    return TimingGraph(
        parse_sdf_file(DATA_DIR / "sky130_lc_iobuf.sdf"), traverse_registers=False
    )


def _random_graph(seed: int) -> TimingGraph:
    rng = random.Random(seed)
    pins = [f"n{i}" for i in range(10)]
    arcs = " ".join(
        f"(INTERCONNECT {a} {b} ({rng.randint(0, 5)}) ({rng.randint(0, 5)}))"
        for i, a in enumerate(pins)
        for b in pins[i + 1 :]
        if rng.random() < 0.4
    )
    return TimingGraph(
        parse_sdf(
            '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "T") (INSTANCE)'
            f" (DELAY (ABSOLUTE (INTERCONNECT n0 n9 (9)) {arcs}))))"
        )
    )


def _uncached(g: nx.MultiDiGraph) -> pathgraph._Searches:  # noqa: SLF001
    """A fresh record that keeps nothing and passes every transition."""
    searches = pathgraph._Searches(g)  # noqa: SLF001
    searches.timed_x = True
    return searches


def _queries(graph: TimingGraph) -> list[object]:
    """Every register, critical and fastest path query on each instance."""
    results: list[object] = []
    for instance in _INSTANCES:
        view = graph.within(instance)
        pins = list(view.graph)
        comb = view.combinational()
        for late, early in _CORNERS:
            for register in view.register_inputs():
                for clock in pins:
                    for source in pins:
                        results.append(
                            view.register_input_timing(
                                source, clock, register, late=late, early=early
                            )
                        )
        for source in pins:
            for sink in pins:
                results.append(critical_path(comb, source, sink))
                results.append(fastest_path(comb, source, sink, corner=Corner.MAX))
        for register_clock in {r.clock for r in view.register_inputs()}:
            for source in pins:
                for sink in pins:
                    results.append(
                        critical_path(view, source, sink, through=register_clock)
                    )
    return results


def test_kept_results_equal_a_fresh_search(
    sky130: TimingGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = _queries(sky130)
    monkeypatch.setattr(pathgraph, "_searches", _uncached)
    fresh = _queries(sky130)
    assert kept == fresh
    assert sum(result is not None for result in kept) > 1000


@pytest.mark.parametrize("seed", range(10))
@pytest.mark.parametrize(
    ("analysis", "edge"),
    [
        pytest.param(critical_path, EdgeSelect.WORST, id="critical-worst"),
        pytest.param(critical_path, EdgeSelect.RISE, id="critical-rise"),
        pytest.param(fastest_path, EdgeSelect.BEST, id="fastest-best"),
    ],
)
def test_paths_equal_a_fresh_search(
    seed: int, analysis, edge: EdgeSelect, monkeypatch: pytest.MonkeyPatch
) -> None:
    graph = _random_graph(seed)
    pins = sorted(graph.nodes())
    pairs = [(a, b) for a in pins for b in pins]
    kept = [analysis(graph, a, b, edge=edge) for a, b in pairs]
    monkeypatch.setattr(pathgraph, "_searches", _uncached)
    assert kept == [analysis(graph, a, b, edge=edge) for a, b in pairs]


def test_cone_order_is_the_networkx_topological_order(sky130: TimingGraph) -> None:
    checked = 0
    for instance in _INSTANCES:
        g = sky130.within(instance).graph
        searches = pathgraph._searches(g)  # noqa: SLF001
        for source in g:
            downstream = nx.descendants(g, source)
            for sink in downstream:
                cone = (downstream | {source}) & (nx.ancestors(g, sink) | {sink})
                expected = list(nx.topological_sort(g.subgraph(cone)))
                assert searches.cone_order(source, sink) == expected
                checked += 1
    assert checked > 100


@pytest.mark.parametrize(
    ("source", "sink"), [("a", "c"), ("b", "c"), ("a", "b")], ids=str
)
def test_a_loop_in_the_cone_still_raises(source: str, sink: str) -> None:
    graph = TimingGraph(
        parse_sdf(
            '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "T") (INSTANCE)'
            " (DELAY (ABSOLUTE (INTERCONNECT a b (1)) (INTERCONNECT b a (1))"
            " (INTERCONNECT b c (1))))))"
        )
    )
    for _ in range(2):
        with pytest.raises(TimingLoopError) as info:
            critical_path(graph, source, sink)
        assert set(info.value.cycle) == {"a", "b"}


@pytest.mark.parametrize(
    "derive",
    [
        pytest.param(lambda g: g, id="built"),
        pytest.param(lambda g: g.within("Inst_A_IOBUF"), id="within"),
        pytest.param(lambda g: g.combinational(), id="combinational"),
    ],
)
def test_every_graph_is_frozen(sky130: TimingGraph, derive) -> None:
    graph = derive(sky130).graph
    assert nx.is_frozen(graph)
    with pytest.raises(nx.NetworkXError, match="Frozen"):
        graph.add_edge("x", "y")
