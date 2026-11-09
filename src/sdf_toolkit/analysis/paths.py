"""Critical, fastest and ranked paths, slack and batch analysis over a timing graph.

Each function takes a {class}`~sdf_toolkit.core.pathgraph.TimingGraph`.

On an acyclic cone, {func}`critical_path`, {func}`compute_slack` and
{func}`batch_endpoint_analysis` run one topological longest-path pass per
signal transition, in time linear in the cone.  A path delay composes
transition by transition, and the maximum over paths of the maximum over
transitions equals the maximum over transitions of the longest path, so RISE,
FALL and WORST are exact.  BEST maximises a minimum over transitions, which
has no longest-path form, and these functions reject it.  A cone with a
loop, such as the IEEE 1497 example 1 design, raises
{class}`~sdf_toolkit.core.pathgraph.TimingLoopError` naming one cycle, since
the longest simple path of a graph with a cycle is NP-hard and these functions
do not approximate it.  With the default ``traverse_registers``, a register
whose output feeds back to its own data input closes such a loop.  A search
through a loop needs a source and sink whose cone avoids it.

{func}`rank_paths` and {func}`verify_path` enumerate every simple path, loops
included, as {meth}`~sdf_toolkit.core.pathgraph.TimingGraph.find_paths` does.
{func}`critical_path` and {func}`fastest_path` take a pin to pass through,
which gives clock-to-output delays through a register's clock pin.
"""

from dataclasses import dataclass
from decimal import Decimal

import networkx as nx

from sdf_toolkit.core.enums import Corner, EdgeSelect, Transition
from sdf_toolkit.core.pathgraph import (
    TimingEdge,
    TimingGraph,
    TimingLoopError,
    _arrivals,
    _cycle,
    _extreme_path,
    _path_counts,
    _require_node,
    _require_path_ends,
    _transitions,
)
from sdf_toolkit.core.values import (
    DEFAULT_TOLERANCE,
    DelayValues,
    MissingDelayError,
    Number,
    to_decimal,
)


@dataclass
class RankedPath:
    """A path with its composed delay and a scalar for ranking.

    Attributes
    ----------
    edges : list[TimingEdge]
        The ordered sequence of timing edges forming the path.
    delay : DelayValues
        The composed delay along the path.
    scalar : Decimal | None
        The extracted scalar value used for ranking.
    """

    edges: list[TimingEdge]
    delay: DelayValues
    scalar: Decimal | None


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
    tolerance : Decimal
        The tolerance used for comparison.
    """

    source: str
    sink: str
    expected: DelayValues
    actual: list[DelayValues]
    passed: bool
    tolerance: Decimal


def verify_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    expected: DelayValues,
    tolerance: Number = DEFAULT_TOLERANCE,
) -> VerificationResult:
    """Check whether any path from *source* to *sink* has the *expected* delay.

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
    tolerance : Number, optional
        Absolute tolerance of the comparison, by default
        {data}`~sdf_toolkit.core.values.DEFAULT_TOLERANCE`.

    Returns
    -------
    VerificationResult
        The verification result containing expected, actual, and pass/fail.
    """
    actual = graph.compose(source, sink)
    tolerance = to_decimal(tolerance)
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
) -> Decimal | None:
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
    """Enumerate every path from *source* to *sink*, sorted by scalar delay.

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
    ranked: list[RankedPath] = []
    for edges in graph.find_paths(source, sink):
        delay = graph.compose_delay(edges)
        scalar = _scalar_or_none(delay, corner, edge)
        ranked.append(RankedPath(edges=edges, delay=delay, scalar=scalar))

    def _sort_key(rp: RankedPath) -> tuple[int, Decimal]:
        if rp.scalar is None:
            # (1, ...) sorts after every (0, ...) whichever direction is asked.
            return (1, Decimal(0))
        return (0, -rp.scalar if descending else rp.scalar)

    ranked.sort(key=_sort_key)
    return ranked


def _ranked(
    graph: TimingGraph, found: tuple[Decimal, list[TimingEdge]] | None
) -> RankedPath | None:
    """Return *found* as a RankedPath, or None if it is missing or has no arcs."""
    if found is None or not found[1]:
        return None
    scalar, edges = found
    return RankedPath(edges=edges, delay=graph.compose_delay(edges), scalar=scalar)


def critical_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
    through: str | None = None,
) -> RankedPath | None:
    """Return the path with the largest scalar delay.

    An acyclic cone takes one longest-path pass per transition; a cone with a
    loop raises.  With *through*, only paths that pass that pin count, such
    as the paths from a clock source through one register's clock pin to an
    output.  A graph built with ``traverse_registers`` may cross a register's
    setup arc on the way; build it without them to follow clock-to-output
    arcs only.

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
    through : str | None
        A pin every path must pass, or None for any path.  It may be
        *source* or *sink*.

    Returns
    -------
    RankedPath | None
        The critical (slowest) path, or None if no path of at least one arc
        from *source* to *sink* has a value at *corner*.

    Raises
    ------
    TimingGraphError
        If *edge* is BEST.
    TimingLoopError
        If a path from *source* to *sink* passes through a loop.
    networkx.NodeNotFound
        If *source*, *sink* or *through* is not a pin of the graph.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.values import DelayValues, Values
    >>> from sdf_toolkit.core.enums import Divider
    >>> from sdf_toolkit.core.delays import PortSpec
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(divider=Divider.SLASH)
    ...     .add_cell("BUF", "b")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_cell("TOP", "")
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
    Decimal('4.5')
    """
    return _ranked(
        graph,
        _extreme_path(
            graph.graph,
            source,
            sink,
            corner=corner,
            edge=edge,
            slowest=True,
            through=through,
        ),
    )


def fastest_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    corner: Corner = Corner.MIN,
    edge: EdgeSelect = EdgeSelect.BEST,
    through: str | None = None,
) -> RankedPath | None:
    """Return the path with the smallest scalar delay, the earliest arrival.

    The counterpart of {func}`critical_path`: one shortest-path pass per
    transition over the acyclic cone, so RISE, FALL and BEST are exact and
    WORST, which minimises a maximum over transitions, is rejected.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph.
    source : str
        The source node name.
    sink : str
        The sink node name.
    corner : Corner
        Process corner to read, by default MIN.
    edge : EdgeSelect
        How the rvalues of the composed delay reduce to one number, by
        default BEST.
    through : str | None
        A pin every path must pass, or None for any path.

    Returns
    -------
    RankedPath | None
        The fastest path, or None if no path of at least one arc from
        *source* to *sink* has a value at *corner*.

    Raises
    ------
    TimingGraphError
        If *edge* is WORST.
    TimingLoopError
        If a path from *source* to *sink* passes through a loop.
    networkx.NodeNotFound
        If *source*, *sink* or *through* is not a pin of the graph.

    Examples
    --------
    >>> from sdf_toolkit.parser.parser import parse_sdf
    >>> sdf = parse_sdf(
    ...     '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "TOP") (INSTANCE)'
    ...     " (DELAY (ABSOLUTE (INTERCONNECT a b (1:2:3)) (INTERCONNECT b c (1:2:3))"
    ...     " (INTERCONNECT a c (2.5:2.5:2.5))))))"
    ... )
    >>> fastest_path(TimingGraph(sdf), "a", "c").scalar
    Decimal('2')
    >>> fastest_path(TimingGraph(sdf), "a", "c", corner=Corner.MAX).scalar
    Decimal('2.5')
    """
    return _ranked(
        graph,
        _extreme_path(
            graph.graph,
            source,
            sink,
            corner=corner,
            edge=edge,
            slowest=False,
            through=through,
        ),
    )


def compute_slack(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    period: Number,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> Decimal | None:
    """Compute the slack for a path: ``period - critical_path.scalar``.

    Parameters
    ----------
    graph : TimingGraph
        The timing graph.
    source : str
        The source node name.
    sink : str
        The sink node name.
    period : Number
        The clock period or timing constraint, converted by
        {func}`~sdf_toolkit.core.values.to_decimal`.
    corner : Corner
        Process corner to read.
    edge : EdgeSelect
        How the rvalues of the composed delay reduce to one number.

    Returns
    -------
    Decimal | None
        The slack, or None if no path from *source* to *sink* has a value at
        *corner*.

    Raises
    ------
    TimingGraphError
        If *edge* is BEST.
    TimingLoopError
        If a path from *source* to *sink* passes through a loop.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.values import DelayValues, Values
    >>> from sdf_toolkit.core.enums import Divider
    >>> from sdf_toolkit.core.delays import PortSpec
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(divider=Divider.SLASH)
    ...     .add_cell("BUF", "b")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_cell("TOP", "")
    ...     .add_interconnect(
    ...         source=PortSpec("a/Y"),
    ...         load=PortSpec("b/A"),
    ...         delay=DelayValues.of(Values(min=0.5, typ=1.0, max=1.5)),
    ...     )
    ...     .build()
    ... )
    >>> g = TimingGraph(sdf)
    >>> compute_slack(g, "a/Y", "b/Y", period=Decimal(10))
    Decimal('5.5')
    """
    found = _extreme_path(
        graph.graph, source, sink, corner=corner, edge=edge, slowest=True
    )
    if found is None or not found[1]:
        return None
    return to_decimal(period) - found[0]


@dataclass
class EndpointResult:
    """Result of analyzing a single source-to-sink endpoint pair.

    Attributes
    ----------
    source : str
        The startpoint pin name.
    sink : str
        The endpoint pin name.
    critical_delay : Decimal | None
        The scalar delay of the critical path, or None if no path has a value.
    path_count : int
        Number of paths between source and sink, parallel arcs counted apart.
    """

    source: str
    sink: str
    critical_delay: Decimal | None
    path_count: int


def _batch_longest(
    graph: nx.MultiDiGraph,
    *,
    sources: list[str],
    sinks: list[str],
    transitions: tuple[Transition, ...],
    corner: Corner,
) -> list[EndpointResult]:
    """One longest-path pass per source and transition over an acyclic graph."""
    order = list(nx.topological_sort(graph))
    position = {node: i for i, node in enumerate(order)}
    results: list[EndpointResult] = []
    for src in sources:
        downstream = order[position[src] :]
        counts = _path_counts(graph, source=src, order=downstream)
        arrivals = [
            _arrivals(graph, source=src, order=downstream, transition=t, corner=corner)
            for t in transitions
        ]
        for snk in sinks:
            if snk == src or snk not in counts:
                continue
            times = [a[snk][0] for a in arrivals if snk in a]
            results.append(
                EndpointResult(
                    source=src,
                    sink=snk,
                    critical_delay=max(times) if times else None,
                    path_count=counts[snk],
                )
            )
    return results


def _batch_by_cone(
    graph: nx.MultiDiGraph,
    *,
    sources: list[str],
    sinks: list[str],
    transitions: tuple[Transition, ...],
    corner: Corner,
) -> list[EndpointResult]:
    """One longest-path pass per pair and transition over the pair's cone.

    A graph with a loop has no topological order, but the cone of a pair
    whose paths avoid the loop does.
    """
    results: list[EndpointResult] = []
    for src in sources:
        downstream = nx.descendants(graph, src)
        for snk in sinks:
            if snk == src or snk not in downstream:
                continue
            cone = graph.subgraph(
                (downstream | {src}) & (nx.ancestors(graph, snk) | {snk})
            )
            if not nx.is_directed_acyclic_graph(cone):
                raise TimingLoopError(cycle=_cycle(cone), source=src, sink=snk)
            order = list(nx.topological_sort(cone))
            arrivals = [
                _arrivals(graph, source=src, order=order, transition=t, corner=corner)
                for t in transitions
            ]
            times = [a[snk][0] for a in arrivals if snk in a]
            results.append(
                EndpointResult(
                    source=src,
                    sink=snk,
                    critical_delay=max(times) if times else None,
                    path_count=_path_counts(graph, source=src, order=order)[snk],
                )
            )
    return results


def batch_endpoint_analysis(
    graph: TimingGraph,
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
    sources: list[str] | None = None,
    sinks: list[str] | None = None,
) -> list[EndpointResult]:
    """Analyze every connected source-to-sink pair.

    An acyclic graph takes one longest-path pass per source and transition.
    A graph with a loop takes one pass per pair over the pair's cone, and
    raises for the first pair whose cone holds the loop.

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

    Raises
    ------
    TimingGraphError
        If *edge* is BEST.
    TimingLoopError
        If a path of a source and sink pair passes through a loop, or if
        *sources* or *sinks* is not given and the graph has no startpoint or
        no endpoint, which leaves every pin on or after a loop.
    networkx.NodeNotFound
        If a given source or sink is not a pin of the graph.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.values import DelayValues, Values
    >>> from sdf_toolkit.core.enums import Divider
    >>> from sdf_toolkit.core.delays import PortSpec
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(divider=Divider.SLASH)
    ...     .add_cell("BUF", "b")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_cell("TOP", "")
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
    Decimal('4.5')
    """
    transitions = _transitions(edge)
    g = graph.graph
    if sources is None:
        sources = sorted(graph.startpoints())
        _require_path_ends(g, sources)
    if sinks is None:
        sinks = sorted(graph.endpoints())
        _require_path_ends(g, sinks)
    for node in (*sources, *sinks):
        _require_node(g, node)

    if nx.is_directed_acyclic_graph(g):
        results = _batch_longest(
            g, sources=sources, sinks=sinks, transitions=transitions, corner=corner
        )
    else:
        results = _batch_by_cone(
            g, sources=sources, sinks=sinks, transitions=transitions, corner=corner
        )

    def _sort_key(r: EndpointResult) -> tuple[int, Decimal]:
        if r.critical_delay is None:
            return (1, Decimal(0))
        return (0, -r.critical_delay)

    results.sort(key=_sort_key)
    return results


def decompose_delay(total: DelayValues, known: DelayValues) -> DelayValues:
    """Return the unknown delay segment, ``total - known``."""
    return total - known
