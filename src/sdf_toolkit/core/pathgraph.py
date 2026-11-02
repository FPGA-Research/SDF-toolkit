"""Timing path graph module for SDF timing analysis.

Builds a directed multigraph from parsed SDF data and provides methods
for path finding, delay composition, and verification.
"""

import functools
import itertools
import operator
from dataclasses import dataclass

import networkx as nx

from sdf_toolkit.core.model import (
    Corner,
    DelayValues,
    EdgeSelect,
    EntryType,
    Interconnect,
    Iopath,
    MissingDelayError,
    SDFFile,
    Setup,
    SetupHold,
)


@dataclass(frozen=True)
class TimingEdge:
    """A single directed timing edge between two pins.

    Attributes
    ----------
    source : str
        Fully-qualified source pin name (e.g., ``"B1/C1/i"``).
    sink : str
        Fully-qualified sink pin name (e.g., ``"B1/C1/z"``).
    delay : DelayValues
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
    delay: DelayValues
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
    delay : DelayValues
        The composed delay along the path.
    scalar : float | None
        The extracted scalar value used for ranking.
    """

    edges: list[TimingEdge]
    delay: DelayValues
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
    expected : DelayValues
        The expected delay for the path.
    actual : list[DelayValues]
        The actual composed delays for all paths found.
    passed : bool
        Whether any actual delay matches the expected within tolerance.
    tolerance : float
        The tolerance used for comparison.
    """

    source: str
    sink: str
    expected: DelayValues
    actual: list[DelayValues]
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
    traverse_registers : bool, optional
        When True, every SETUP/SETUPHOLD timing check additionally adds a
        data-pin to clock-pin edge whose delay is the check's setup time.
        Sequential cells then become traversable: a path can continue
        from a register's data input through its clock pin and on through
        the cell's clock-to-output IOPATH. By default True, which makes
        registers traversable so timing paths may cross sequential cells.
    """

    def __init__(self, sdf: SDFFile, traverse_registers: bool = True) -> None:
        self.divider = sdf.header.effective_divider
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._build(sdf, traverse_registers)

    def _build(self, sdf: SDFFile, traverse_registers: bool) -> None:
        """Populate the graph from SDF cells.

        For each cell in the SDF file, IOPATH and INTERCONNECT entries
        are converted to directed edges. IOPATH pin names are qualified
        with the instance hierarchy path, while INTERCONNECT pin names
        are used as-is (they are already fully qualified). With
        ``traverse_registers``, SETUP/SETUPHOLD checks are converted to
        data-to-clock edges as well.

        Parameters
        ----------
        sdf : SDFFile
            The parsed SDF file.
        traverse_registers : bool
            Whether to add data-to-clock edges for setup-style checks.
        """
        divider = sdf.header.effective_divider

        for cell_type, instances in sdf.cells.items():
            for instance, entries in instances.items():
                for entry in entries:
                    if traverse_registers and isinstance(entry, Setup | SetupHold):
                        self._add_register_edge(entry, cell_type, instance, divider)
                        continue

                    if isinstance(entry, Interconnect):
                        source = entry.source.port
                        sink = entry.load.port
                    elif isinstance(entry, Iopath):
                        source = _qualify_pin(instance, entry.input.port, divider)
                        sink = _qualify_pin(instance, entry.output.port, divider)
                    else:
                        continue

                    self._graph.add_edge(
                        source,
                        sink,
                        delay=entry.delay,
                        entry_type=entry.type,
                        cell_type=cell_type,
                        instance=instance,
                    )

    def _add_register_edge(
        self,
        entry: Setup | SetupHold,
        cell_type: str,
        instance: str,
        divider: str,
    ) -> None:
        """Add a data-to-clock edge for a setup-style timing check.

        The edge runs from the check's data port to its reference (clock)
        port and its delay is the setup time, the physically meaningful
        forward cost of crossing the register boundary (data must be stable
        that long before the clock edge; propagation then continues through
        the cell's clock-to-output IOPATH). HOLD checks add no edge: hold is
        a minimum-arrival constraint, not a propagation cost, and its
        typically negative values would corrupt path delay sums.

        SDF allows several setup checks per pin pair (edge or condition
        variants); each becomes its own parallel edge, consistent with
        how conditional IOPATH variants are kept as parallel edges.

        Parameters
        ----------
        entry : Setup | SetupHold
            A SETUP or SETUPHOLD timing check entry.
        cell_type : str
            The cell type from the SDF file.
        instance : str
            The instance name from the SDF file.
        divider : str
            The hierarchy divider character.
        """
        # Only the setup half of a SETUPHOLD is a forward cost.
        setup_values = entry.limit if isinstance(entry, Setup) else entry.setup
        data_pin = _qualify_pin(instance, entry.data.port, divider)
        clock_pin = _qualify_pin(instance, entry.reference.port, divider)
        self._graph.add_edge(
            data_pin,
            clock_pin,
            delay=DelayValues.of(setup_values),
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
        if source not in self._graph:
            raise nx.NodeNotFound(f"Source node {source!r} not in graph")
        if sink not in self._graph:
            raise nx.NodeNotFound(f"Sink node {sink!r} not in graph")
        if source == sink:
            return []

        edge_paths: list[list[TimingEdge]] = []

        # nx.all_simple_paths on MultiDiGraph may yield duplicate node
        # sequences (one per parallel-edge combination).  Deduplicate
        # so the product expansion below counts each edge combo once.
        seen_node_paths: set[tuple[str, ...]] = set()
        for node_path in nx.all_simple_paths(
            self._graph, source, sink, cutoff=max_depth
        ):
            key = tuple(node_path)
            if key in seen_node_paths:
                continue
            seen_node_paths.add(key)

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

    def compose_delay(self, path: list[TimingEdge]) -> DelayValues:
        """Sum the delays along a path of timing edges.

        Parameters
        ----------
        path : list[TimingEdge]
            An ordered sequence of timing edges forming a path.

        Returns
        -------
        DelayValues
            The total composed delay along the path.

        Raises
        ------
        ValueError
            If the path is empty.
        """
        if not path:
            raise ValueError("Cannot compose delay for an empty path.")

        return functools.reduce(operator.add, (edge.delay for edge in path))

    def compose(self, source: str, sink: str) -> list[DelayValues]:
        """Find all paths and return their composed delays.

        Parameters
        ----------
        source : str
            The source node name.
        sink : str
            The sink node name.

        Returns
        -------
        list[DelayValues]
            Composed delays for each path found between source and sink.
        """
        paths = self.find_paths(source, sink)
        return [self.compose_delay(p) for p in paths]


def verify_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    expected: DelayValues,
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
    expected : DelayValues
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


def _scalar_or_none(
    delay: DelayValues, corner: Corner, edge: EdgeSelect
) -> float | None:
    """Reduce *delay* to a scalar, or None where the file gives no value."""
    try:
        return delay.scalar(corner=corner, edge=edge)
    except MissingDelayError:
        return None


def rank_paths(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
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
    corner : Corner
        Process corner to read.
    edge : EdgeSelect
        How the rvalues of the composed delay reduce to one number.
    descending : bool
        If True, sort largest scalar first. Paths without a value go last.

    Returns
    -------
    list[RankedPath]
        Ranked list of paths.
    """
    edge_paths = graph.find_paths(source, sink)
    ranked: list[RankedPath] = []
    for edges in edge_paths:
        delay = graph.compose_delay(edges)
        scalar = _scalar_or_none(delay, corner, edge)
        ranked.append(RankedPath(edges=edges, delay=delay, scalar=scalar))

    def _sort_key(rp: RankedPath) -> tuple[int, float]:
        if rp.scalar is None:
            # Always sort None last: use (1, ...) so it comes after (0, ...)
            # regardless of reverse flag.
            return (1, 0.0)
        sign = -1.0 if descending else 1.0
        return (0, sign * rp.scalar)

    ranked.sort(key=_sort_key)
    return ranked


def critical_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
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
    corner : Corner
        Process corner to read.
    edge : EdgeSelect
        How the rvalues of the composed delay reduce to one number.

    Returns
    -------
    RankedPath | None
        The critical (slowest) path, or None if no paths exist.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.model import DelayValues, Divider, PortSpec, Values
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(divider=Divider.SLASH)
    ...     .add_cell("BUF", "b")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_interconnect(
    ...         source=PortSpec("a/Y"),
    ...         load=PortSpec("b/A"),
    ...         delay=DelayValues.of(Values(min=0.5, typ=1.0, max=1.5)),
    ...     )
    ...     .build()
    ... )
    >>> g = TimingGraph(sdf)
    >>> cp = critical_path(g, "a/Y", "b/Y", corner=Corner.MAX)
    >>> cp.scalar
    4.5
    """
    ranked = rank_paths(graph, source, sink, corner=corner, edge=edge, descending=True)
    return ranked[0] if ranked else None


def compute_slack(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    period: float,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
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
    corner : Corner
        Process corner to read.
    edge : EdgeSelect
        How the rvalues of the composed delay reduce to one number.

    Returns
    -------
    float | None
        The slack, or None if no path exists or it has no value to compare.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.model import DelayValues, Divider, PortSpec, Values
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(divider=Divider.SLASH)
    ...     .add_cell("BUF", "b")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_interconnect(
    ...         source=PortSpec("a/Y"),
    ...         load=PortSpec("b/A"),
    ...         delay=DelayValues.of(Values(min=0.5, typ=1.0, max=1.5)),
    ...     )
    ...     .build()
    ... )
    >>> g = TimingGraph(sdf)
    >>> compute_slack(g, "a/Y", "b/Y", period=10.0)
    5.5
    """
    cp = critical_path(graph, source, sink, corner=corner, edge=edge)
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
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
    sources: list[str] | None = None,
    sinks: list[str] | None = None,
) -> list[EndpointResult]:
    """Analyze all startpoint-to-endpoint pairs in a timing graph.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph to analyze.
    corner : Corner
        Process corner to read.
    edge : EdgeSelect
        How the rvalues of a composed delay reduce to one number.
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
    >>> from sdf_toolkit.core.model import DelayValues, Divider, PortSpec, Values
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(divider=Divider.SLASH)
    ...     .add_cell("BUF", "b")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_interconnect(
    ...         source=PortSpec("a/Y"),
    ...         load=PortSpec("b/A"),
    ...         delay=DelayValues.of(Values(min=0.5, typ=1.0, max=1.5)),
    ...     )
    ...     .build()
    ... )
    >>> g = TimingGraph(sdf)
    >>> results = batch_endpoint_analysis(g, corner=Corner.MAX)
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
                if (s := _scalar_or_none(graph.compose_delay(p), corner, edge))
                is not None
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


def decompose_delay(total: DelayValues, known: DelayValues) -> DelayValues:
    """Compute the unknown delay segment by subtracting known from total.

    Parameters
    ----------
    total : DelayValues
        The total end-to-end delay.
    known : DelayValues
        The known portion of the delay.

    Returns
    -------
    DelayValues
        The remaining unknown delay (total - known).
    """
    return total - known
