"""DOT/Graphviz export for timing graphs.

The ``graphviz`` package writes the DOT text, so node names that are DOT
keywords (``node``, ``edge``) or carry quotes are quoted correctly.  A node
ID is the escaped SDF pin path, which is unique; its label is the unescaped
name, passed through ``graphviz.escape`` so a backslash in the name is not
read as a DOT label escape.
"""

import graphviz

from sdf_toolkit.core.identifiers import sdf_unescape, split_path
from sdf_toolkit.core.model import Corner, EdgeSelect, MissingDelayError
from sdf_toolkit.core.pathgraph import RankedPath, TimingGraph

_TOP_CLUSTER_LABEL = "(top)"


def _add_node(target: graphviz.Digraph, node: str) -> None:
    """Add *node* to *target* labelled with its unescaped name."""
    target.node(node, label=graphviz.escape(sdf_unescape(node)))


def to_dot(
    graph: TimingGraph,
    *,
    highlight_path: RankedPath | None = None,
    cluster_by_instance: bool = False,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> str:
    """Export a timing graph to DOT format.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph to export.
    highlight_path : RankedPath | None
        If provided, highlight these edges in red with bold penwidth.
    cluster_by_instance : bool
        If True, group nodes into subgraph clusters by the instance path
        before the last unescaped divider of the SDF.
    corner : Corner
        Process corner for edge labels.
    edge : EdgeSelect
        How each edge's rvalues reduce to one label value.

    Returns
    -------
    str
        The DOT-format string.

    Raises
    ------
    MissingDelayError
        If an edge has no value at *corner* for *edge*.
    ValueError
        If a node name ends in a backslash that escapes nothing.
    """
    highlight_edges: set[tuple[str, str]] = (
        {(e.source, e.sink) for e in highlight_path.edges}
        if highlight_path is not None
        else set()
    )

    dot = graphviz.Digraph("timing", graph_attr={"rankdir": "LR"})
    nodes = sorted(graph.nodes())

    if cluster_by_instance:
        clusters: dict[str, list[str]] = {}
        for node in nodes:
            *instance, _pin = split_path(node, graph.divider)
            clusters.setdefault(graph.divider.join(instance), []).append(node)
        for i, (instance, members) in enumerate(sorted(clusters.items())):
            with dot.subgraph(name=f"cluster_{i}") as cluster:
                label = (
                    graphviz.escape(sdf_unescape(instance))
                    if instance
                    else _TOP_CLUSTER_LABEL
                )
                cluster.attr(label=label)
                for node in members:
                    _add_node(cluster, node)
    else:
        for node in nodes:
            _add_node(dot, node)

    for timing_edge in graph.edges():
        try:
            value = timing_edge.delay.scalar(corner=corner, edge=edge)
        except MissingDelayError as exc:
            msg = f"edge {timing_edge.source} -> {timing_edge.sink}: {exc}"
            raise MissingDelayError(msg) from exc
        attrs = {"label": f"{value:.3f}"}
        if (timing_edge.source, timing_edge.sink) in highlight_edges:
            attrs |= {"color": "red", "penwidth": "2.0"}
        # SDF names cannot hold ':', so graphviz never reads one as node:port.
        dot.edge(timing_edge.source, timing_edge.sink, **attrs)

    return dot.source
