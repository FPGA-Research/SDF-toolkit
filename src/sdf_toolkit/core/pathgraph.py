"""Timing path graph module for SDF timing analysis.

Builds a directed multigraph from parsed SDF data and provides methods
for path finding, delay composition, and verification.
"""

import functools
import itertools
import operator
from dataclasses import dataclass

import networkx as nx

from sdf_toolkit.core.model import DelayField, DelayFieldLike, DelayMetric, DelayMetricLike, DelayPaths, EntryType, SDFFile


@dataclass(frozen=True)
class TimingEdge:
    """A single directed timing edge between two pins.

    Attributes
    ----------
    source : str
        Fully-qualified source pin name (e.g., ``"B1/C1/i"``).
    sink : str
        Fully-qualified sink pin name (e.g., ``"B1/C1/z"``).
    delay : DelayPaths
        The delay associated with this edge.
    entry_type : EntryType
        The SDF entry type (IOPATH, INTERCONNECT, etc.).
    cell_type : str
        The cell type from the SDF file.
    instance : str
        The instance name from the SDF file.
    """

    source: str
    sink: str
    delay: DelayPaths
    entry_type: EntryType
    cell_type: str
    instance: str


@dataclass
class RankedPath:
    """A path with its composed delay and a scalar for ranking.

    Attributes
    ----------
    edges : list[TimingEdge]
        The ordered sequence of timing edges forming the path.
    delay : DelayPaths
        The composed delay along the path.
    scalar : float | None
        The extracted scalar value used for ranking.
    """

    edges: list[TimingEdge]
    delay: DelayPaths
    scalar: float | None


@dataclass
class VerificationResult:
    """Result of verifying a path's composed delay against an expected value.

    Attributes
    ----------
    source : str
        The source pin of the path.
    sink : str
        The sink pin of the path.
    expected : DelayPaths
        The expected delay for the path.
    actual : list[DelayPaths]
        The actual composed delays for all paths found.
    passed : bool
        Whether any actual delay matches the expected within tolerance.
    tolerance : float
        The tolerance used for comparison.
    """

    source: str
    sink: str
    expected: DelayPaths
    actual: list[DelayPaths]
    passed: bool
    tolerance: float


def _qualify_pin(instance: str, pin: str, divider: str) -> str:
    """Qualify a local pin name with its instance path.

    Parameters
    ----------
    instance : str
        The instance hierarchy path (e.g., ``"B1/C1"``).
    pin : str
        The local pin name (e.g., ``"i"``).
    divider : str
        The hierarchy divider character.

    Returns
    -------
    str
        The fully-qualified pin name (e.g., ``"B1/C1/i"``).
    """
    if instance:
        return f"{instance}{divider}{pin}"
    return pin


def _edge_from_attrs(
    source: str,
    sink: str,
    attrs: dict[str, object],
) -> TimingEdge:
    """Construct a TimingEdge from graph edge attribute dict.

    Parameters
    ----------
    source : str
        The source node name.
    sink : str
        The sink node name.
    attrs : dict[str, object]
        The edge attribute dictionary from the NetworkX graph.

    Returns
    -------
    TimingEdge
        The constructed timing edge.
    """
    return TimingEdge(
        source=source,
        sink=sink,
        delay=attrs["delay"],  # type: ignore[arg-type]
        entry_type=attrs["entry_type"],  # type: ignore[arg-type]
        cell_type=attrs["cell_type"],  # type: ignore[arg-type]
        instance=attrs["instance"],  # type: ignore[arg-type]
    )


class TimingGraph:
    """Directed multigraph of timing edges built from an SDF file.

    Wraps a ``networkx.MultiDiGraph`` and provides methods for path
    finding, delay composition, and graph inspection.

    Parameters
    ----------
    sdf : SDFFile
        The parsed SDF file to build the graph from.
    """

    def __init__(self, sdf: SDFFile) -> None:
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._build(sdf)

    def _build(self, sdf: SDFFile) -> None:
        """Populate the graph from SDF cells.

        For each cell in the SDF file, IOPATH and INTERCONNECT entries
        are converted to directed edges. IOPATH pin names are qualified
        with the instance hierarchy path, while INTERCONNECT pin names
        are used as-is (they are already fully qualified).

        Parameters
        ----------
        sdf : SDFFile
            The parsed SDF file.
        """
        divider = sdf.header.divider or "/"

        for cell_type, instances in sdf.cells.items():
            for instance, entries in instances.items():
                for _entry_name, entry in entries.items():
                    if entry.type not in (EntryType.IOPATH, EntryType.INTERCONNECT):
                        continue

                    if entry.from_pin is None or entry.to_pin is None:
                        continue

                    if entry.delay_paths is None:
                        continue

                    if entry.type == EntryType.INTERCONNECT:
                        source = entry.from_pin
                        sink = entry.to_pin
                    else:
                        source = _qualify_pin(instance, entry.from_pin, divider)
                        sink = _qualify_pin(instance, entry.to_pin, divider)

                    self._graph.add_edge(
                        source,
                        sink,
                        delay=entry.delay_paths,
                        entry_type=entry.type,
                        cell_type=cell_type,
                        instance=instance,
                    )

    @property
    def graph(self) -> nx.MultiDiGraph:
        """Expose the underlying NetworkX MultiDiGraph for advanced analysis.

        Returns
        -------
        nx.MultiDiGraph
            The backing graph.
        """
        return self._graph

    def nodes(self) -> set[str]:
        """Return all node names in the graph.

        Returns
        -------
        set[str]
            Set of fully-qualified pin names.
        """
        return set(self._graph.nodes())

    def startpoints(self) -> set[str]:
        """Return nodes with in-degree 0 (primary inputs).

        Returns
        -------
        set[str]
            Set of node names that have no incoming edges.
        """
        return {n for n, d in self._graph.in_degree() if d == 0}

    def endpoints(self) -> set[str]:
        """Return nodes with out-degree 0 (primary outputs).

        Returns
        -------
        set[str]
            Set of node names that have no outgoing edges.
        """
        return {n for n, d in self._graph.out_degree() if d == 0}

    def edges(self) -> list[TimingEdge]:
        """Return all edges in the graph as TimingEdge objects.

        Returns
        -------
        list[TimingEdge]
            All timing edges in the graph.
        """
        return [
            _edge_from_attrs(u, v, attrs)
            for u, v, _key, attrs in self._graph.edges(data=True, keys=True)
        ]

    def successors(self, node: str) -> list[TimingEdge]:
        """Return all outgoing edges from a node.

        Parameters
        ----------
        node : str
            The source node name.

        Returns
        -------
        list[TimingEdge]
            Outgoing timing edges from the node.
        """
        return [
            _edge_from_attrs(u, v, attrs)
            for u, v, _key, attrs in self._graph.out_edges(node, data=True, keys=True)
        ]

    def predecessors(self, node: str) -> list[TimingEdge]:
        """Return all incoming edges to a node.

        Parameters
        ----------
        node : str
            The sink node name.

        Returns
        -------
        list[TimingEdge]
            Incoming timing edges to the node.
        """
        return [
            _edge_from_attrs(u, v, attrs)
            for u, v, _key, attrs in self._graph.in_edges(node, data=True, keys=True)
        ]

    def find_paths(
        self,
        source: str,
        sink: str,
        max_depth: int = 50,
    ) -> list[list[TimingEdge]]:
        """Find all simple paths between source and sink as edge sequences.

        Uses ``nx.all_simple_paths`` to find node paths, then converts
        each to a sequence of TimingEdge objects. For MultiDiGraph edges,
        all combinations of parallel edges are enumerated via Cartesian
        product.

        Parameters
        ----------
        source : str
            The source node name.
        sink : str
            The sink node name.
        max_depth : int, optional
            Maximum path length (number of edges), by default 50.

        Returns
        -------
        list[list[TimingEdge]]
            All simple paths as lists of TimingEdge objects.
        """
        edge_paths: list[list[TimingEdge]] = []

        for node_path in nx.all_simple_paths(
            self._graph, source, sink, cutoff=max_depth
        ):
            hop_options: list[list[TimingEdge]] = []
            for u, v in itertools.pairwise(node_path):
                edge_dict = self._graph[u][v]
                hop_edges = [
                    _edge_from_attrs(u, v, attrs) for attrs in edge_dict.values()
                ]
                hop_options.append(hop_edges)

            for combination in itertools.product(*hop_options):
                edge_paths.append(list(combination))

        return edge_paths

    def compose_delay(self, path: list[TimingEdge]) -> DelayPaths:
        """Sum the delays along a path of timing edges.

        Parameters
        ----------
        path : list[TimingEdge]
            An ordered sequence of timing edges forming a path.

        Returns
        -------
        DelayPaths
            The total composed delay along the path.

        Raises
        ------
        ValueError
            If the path is empty.
        """
        if not path:
            raise ValueError("Cannot compose delay for an empty path.")

        return functools.reduce(operator.add, (edge.delay for edge in path))

    def compose(self, source: str, sink: str) -> list[DelayPaths]:
        """Find all paths and return their composed delays.

        Parameters
        ----------
        source : str
            The source node name.
        sink : str
            The sink node name.

        Returns
        -------
        list[DelayPaths]
            Composed delays for each path found between source and sink.
        """
        paths = self.find_paths(source, sink)
        return [self.compose_delay(p) for p in paths]


def verify_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    expected: DelayPaths,
    tolerance: float = 1e-9,
) -> VerificationResult:
    """Verify that a path's composed delay matches an expected value.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph to search.
    source : str
        The source node name.
    sink : str
        The sink node name.
    expected : DelayPaths
        The expected total delay.
    tolerance : float, optional
        Absolute tolerance for floating-point comparison, by default 1e-9.

    Returns
    -------
    VerificationResult
        The verification result containing expected, actual, and pass/fail.
    """
    actual = graph.compose(source, sink)
    passed = any(expected.approx_eq(a, tolerance) for a in actual)
    return VerificationResult(
        source=source,
        sink=sink,
        expected=expected,
        actual=actual,
        passed=passed,
        tolerance=tolerance,
    )


def rank_paths(
    graph: TimingGraph,
    source: str,
    sink: str,
    field: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
    descending: bool = True,
) -> list[RankedPath]:
    """Find all paths between source and sink, sorted by scalar delay.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph.
    source : str
        The source node name.
    sink : str
        The sink node name.
    field : str
        Delay field to extract (nominal, fast, slow, …).
    metric : str
        Metric to extract (min, avg, max).
    descending : bool
        If True, sort largest scalar first. Paths with None scalar go last.

    Returns
    -------
    list[RankedPath]
        Ranked list of paths.
    """
    edge_paths = graph.find_paths(source, sink)
    ranked: list[RankedPath] = []
    for edges in edge_paths:
        delay = graph.compose_delay(edges)
        scalar = delay.get_scalar(field, metric)
        ranked.append(RankedPath(edges=edges, delay=delay, scalar=scalar))

    def _sort_key(rp: RankedPath) -> tuple[int, float]:
        if rp.scalar is None:
            return (1, 0.0)
        return (0, rp.scalar)

    ranked.sort(key=_sort_key, reverse=descending)
    return ranked


def critical_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    field: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
) -> RankedPath | None:
    """Return the path with the largest scalar delay, or None.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph.
    source : str
        The source node name.
    sink : str
        The sink node name.
    field : str
        Delay field to extract.
    metric : str
        Metric to extract.

    Returns
    -------
    RankedPath | None
        The critical (slowest) path, or None if no paths exist.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.pathgraph import TimingGraph, critical_path
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("BUF", "b")
    ...         .add_iopath("A", "Y", {
    ...             "slow": {"min": 1.0, "avg": 2.0, "max": 3.0},
    ...         })
    ...         .add_interconnect("a/Y", "b/A", {
    ...             "slow": {"min": 0.5, "avg": 1.0, "max": 1.5},
    ...         })
    ...     .build()
    ... )
    >>> g = TimingGraph(sdf)
    >>> cp = critical_path(g, "a/Y", "b/Y", "slow", "max")
    >>> cp.scalar
    4.5
    """
    ranked = rank_paths(graph, source, sink, field, metric, descending=True)
    return ranked[0] if ranked else None


def compute_slack(
    graph: TimingGraph,
    source: str,
    sink: str,
    period: float,
    field: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
) -> float | None:
    """Compute the slack for a path: ``period - critical_path.scalar``.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph.
    source : str
        The source node name.
    sink : str
        The sink node name.
    period : float
        The clock period or timing constraint.
    field : str
        Delay field to extract.
    metric : str
        Metric to extract.

    Returns
    -------
    float | None
        The slack, or None if no critical path or scalar is None.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.pathgraph import TimingGraph, compute_slack
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("BUF", "b")
    ...         .add_iopath("A", "Y", {
    ...             "slow": {"min": 1.0, "avg": 2.0, "max": 3.0},
    ...         })
    ...         .add_interconnect("a/Y", "b/A", {
    ...             "slow": {"min": 0.5, "avg": 1.0, "max": 1.5},
    ...         })
    ...     .build()
    ... )
    >>> g = TimingGraph(sdf)
    >>> compute_slack(g, "a/Y", "b/Y", 10.0, "slow", "max")
    5.5
    """
    cp = critical_path(graph, source, sink, field, metric)
    if cp is None or cp.scalar is None:
        return None
    return period - cp.scalar


@dataclass
class EndpointResult:
    """Result of analyzing a single source-to-sink endpoint pair.

    Attributes
    ----------
    source : str
        The startpoint pin name.
    sink : str
        The endpoint pin name.
    critical_delay : float | None
        The scalar delay of the critical path, or None.
    path_count : int
        Number of distinct paths between source and sink.
    """

    source: str
    sink: str
    critical_delay: float | None
    path_count: int


def batch_endpoint_analysis(
    graph: TimingGraph,
    field: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
    sources: list[str] | None = None,
    sinks: list[str] | None = None,
) -> list[EndpointResult]:
    """Analyze all startpoint-to-endpoint pairs in a timing graph.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph to analyze.
    field : str
        Delay field to extract (nominal, fast, slow, ...).
    metric : str
        Metric to extract (min, avg, max).
    sources : list[str] | None
        Source pins to consider. Defaults to all startpoints.
    sinks : list[str] | None
        Sink pins to consider. Defaults to all endpoints.

    Returns
    -------
    list[EndpointResult]
        Results sorted by critical_delay descending (None last).

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.pathgraph import TimingGraph, batch_endpoint_analysis
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("BUF", "b")
    ...         .add_iopath("A", "Y", {
    ...             "slow": {"min": 1.0, "avg": 2.0, "max": 3.0},
    ...         })
    ...         .add_interconnect("a/Y", "b/A", {
    ...             "slow": {"min": 0.5, "avg": 1.0, "max": 1.5},
    ...         })
    ...     .build()
    ... )
    >>> g = TimingGraph(sdf)
    >>> results = batch_endpoint_analysis(g, "slow", "max")
    >>> len(results)
    1
    >>> results[0].source
    'a/Y'
    >>> results[0].sink
    'b/Y'
    >>> results[0].critical_delay
    4.5
    """
    if sources is None:
        sources = sorted(graph.startpoints())
    if sinks is None:
        sinks = sorted(graph.endpoints())

    results: list[EndpointResult] = []
    for src in sources:
        for snk in sinks:
            paths = graph.find_paths(src, snk)
            if not paths:
                continue

            # Compute critical delay directly from found paths to avoid
            # a redundant second find_paths call via critical_path().
            scalars = [
                s
                for p in paths
                if (s := graph.compose_delay(p).get_scalar(field, metric)) is not None
            ]
            critical_delay = max(scalars) if scalars else None

            results.append(
                EndpointResult(
                    source=src,
                    sink=snk,
                    critical_delay=critical_delay,
                    path_count=len(paths),
                )
            )

    def _sort_key(r: EndpointResult) -> tuple[int, float]:
        if r.critical_delay is None:
            return (1, 0.0)
        return (0, -r.critical_delay)

    results.sort(key=_sort_key)
    return results


def decompose_delay(total: DelayPaths, known: DelayPaths) -> DelayPaths:
    """Compute the unknown delay segment by subtracting known from total.

    Parameters
    ----------
    total : DelayPaths
        The total end-to-end delay.
    known : DelayPaths
        The known portion of the delay.

    Returns
    -------
    DelayPaths
        The remaining unknown delay (total - known).
    """
    return total - known
