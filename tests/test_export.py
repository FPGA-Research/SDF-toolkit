import json
import shutil
from typing import Any

import graphviz
import pytest

from sdf_toolkit.analysis.export import to_dot
from sdf_toolkit.core.model import MissingDelayError
from sdf_toolkit.core.pathgraph import TimingGraph, critical_path
from sdf_toolkit.parser.parser import parse_sdf

needs_dot = pytest.mark.skipif(
    shutil.which("dot") is None, reason="Graphviz dot not installed"
)

# Port names that are DOT keywords, and escaped instance names.
_AWKWARD = r"""(DELAYFILE (SDFVERSION "3.0") (DIVIDER {divider}) (TIMESCALE 1ns)
 (CELL (CELLTYPE "INV") (INSTANCE u\{divider}x\[0\])
  (DELAY (ABSOLUTE (IOPATH node edge {rvalue}))))
 (CELL (CELLTYPE "top") (INSTANCE)
  (DELAY (ABSOLUTE (INTERCONNECT graph u\{divider}x\[0\]{divider}node (2))
   (INTERCONNECT node edge (3)))))
)"""


def _awkward_graph(divider: str, rvalue: str = "(1)") -> TimingGraph:
    return TimingGraph(parse_sdf(_AWKWARD.format(divider=divider, rvalue=rvalue)))


def _layout(source: str) -> dict[str, Any]:
    """Parse *source* with Graphviz itself, which fails on malformed DOT."""
    return json.loads(graphviz.pipe_string("dot", "json0", source, encoding="utf-8"))


def _nodes(layout: dict[str, Any]) -> dict[str, str]:
    """Return {node name: label} for every node, leaving clusters out."""
    return {o["name"]: o["label"] for o in layout["objects"] if "nodes" not in o}


def _clusters(layout: dict[str, Any]) -> dict[str, set[str]]:
    """Return {cluster label: member node names}."""
    objects = layout["objects"]
    return {
        o["label"]: {objects[i]["name"] for i in o["nodes"]}
        for o in objects
        if "nodes" in o
    }


class TestToDotText:
    """Checks on the DOT text that need no Graphviz binary."""

    @pytest.mark.parametrize(
        "fragment", ["digraph timing {", "rankdir=LR", '"node"', '"edge"', '"graph"']
    )
    def test_keywords_are_quoted(self, fragment: str) -> None:
        assert fragment in to_dot(_awkward_graph("/"))

    @pytest.mark.parametrize("rvalue", ["()", "(1::)"])
    def test_missing_value_raises(self, rvalue: str) -> None:
        with pytest.raises(MissingDelayError, match="node"):
            to_dot(_awkward_graph("/", rvalue))


@needs_dot
class TestToDot:
    def test_nodes_and_edges_match_graph(self, spec1_graph: TimingGraph) -> None:
        layout = _layout(to_dot(spec1_graph))
        assert set(_nodes(layout)) == spec1_graph.nodes()
        assert len(layout["edges"]) == len(spec1_graph.edges())
        assert all(e["label"] for e in layout["edges"])

    @pytest.mark.parametrize("divider", ["/", "."])
    def test_awkward_names_round_trip(self, divider: str) -> None:
        graph = _awkward_graph(divider)
        nodes = _nodes(_layout(to_dot(graph)))
        assert set(nodes) == graph.nodes()
        assert nodes == {
            "graph": "graph",
            "node": "node",
            "edge": "edge",
            f"u\\{divider}x\\[0\\]{divider}node": f"u{divider}x[0]{divider}node",
            f"u\\{divider}x\\[0\\]{divider}edge": f"u{divider}x[0]{divider}edge",
        }


@needs_dot
class TestToDotHighlight:
    def test_highlight_path(self, spec2_graph: TimingGraph) -> None:
        cp = critical_path(spec2_graph, "top.ff1.CP", "top.ff1.QN")
        assert cp is not None
        on_path = {(e.source, e.sink) for e in cp.edges}
        layout = _layout(to_dot(spec2_graph, highlight_path=cp))
        names = [o["name"] for o in layout["objects"]]
        red = {
            (names[e["tail"]], names[e["head"]])
            for e in layout["edges"]
            if e.get("color") == "red"
        }
        assert red == on_path

    def test_no_highlight(self, spec1_graph: TimingGraph) -> None:
        layout = _layout(to_dot(spec1_graph, highlight_path=None))
        assert not any("color" in e for e in layout["edges"])


@needs_dot
class TestToDotCluster:
    @pytest.mark.parametrize("divider", ["/", "."])
    def test_clusters_split_on_unescaped_divider(self, divider: str) -> None:
        layout = _layout(to_dot(_awkward_graph(divider), cluster_by_instance=True))
        assert _clusters(layout) == {
            "(top)": {"graph", "node", "edge"},
            f"u{divider}x[0]": {
                f"u\\{divider}x\\[0\\]{divider}node",
                f"u\\{divider}x\\[0\\]{divider}edge",
            },
        }

    def test_no_cluster(self, spec1_graph: TimingGraph) -> None:
        layout = _layout(to_dot(spec1_graph, cluster_by_instance=False))
        assert _clusters(layout) == {}

    def test_cluster_with_highlight(self, spec2_graph: TimingGraph) -> None:
        cp = critical_path(spec2_graph, "top.ff1.CP", "top.ff1.QN")
        layout = _layout(
            to_dot(spec2_graph, highlight_path=cp, cluster_by_instance=True)
        )
        assert _clusters(layout)
        assert any(e.get("color") == "red" for e in layout["edges"])
