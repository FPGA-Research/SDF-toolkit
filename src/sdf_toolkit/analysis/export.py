"""DOT/Graphviz export for timing graphs."""

from sdf_toolkit.core.model import Corner, EdgeSelect, MissingDelayError
from sdf_toolkit.core.pathgraph import RankedPath, TimingGraph


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
        If True, group nodes into subgraph clusters by instance prefix.
    corner : Corner
        Process corner for edge labels.
    edge : EdgeSelect
        How each edge's rvalues reduce to one label value.

    Returns
    -------
    str
        The DOT-format string.
    """
    highlight_edges: set[tuple[str, str]] = (
        {(edge.source, edge.sink) for edge in highlight_path.edges}
        if highlight_path is not None
        else set()
    )

    lines: list[str] = ["digraph timing {", "  rankdir=LR;"]

    nodes = graph.nodes()

    if cluster_by_instance:
        clusters: dict[str, list[str]] = {}
        for node in sorted(nodes):
            parts = node.rsplit("/", 1)
            instance = parts[0] if len(parts) > 1 else ""
            clusters.setdefault(instance, []).append(node)
        for i, (instance, members) in enumerate(sorted(clusters.items())):
            label = instance or "(top)"
            lines.append(f"  subgraph cluster_{i} {{")
            lines.append(f'    label="{label}";')
            for node in sorted(members):
                lines.append(f'    "{node}";')
            lines.append("  }")
    else:
        for node in sorted(nodes):
            lines.append(f'  "{node}";')

    for timing_edge in graph.edges():
        try:
            label = f"{timing_edge.delay.scalar(corner=corner, edge=edge):.3f}"
        except MissingDelayError:
            label = "?"
        attrs = f'label="{label}"'
        if (timing_edge.source, timing_edge.sink) in highlight_edges:
            attrs += ', color="red", penwidth=2.0'
        lines.append(f'  "{timing_edge.source}" -> "{timing_edge.sink}" [{attrs}];')

    lines.append("}")
    return "\n".join(lines)
