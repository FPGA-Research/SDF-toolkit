"""Timing graph of an SDF file and longest-path analysis over it.

Nodes are pins named by instance path, the file's divider and port.  Each
IOPATH, INTERCONNECT and (with ``traverse_registers``) setup check becomes one
arc of a ``networkx.MultiDiGraph``, and the arc's {class}`TimingEdge` is the
only attribute stored on it, under {data}`ARC_ATTR`.  INTERCONNECT ports are
scoped to the enclosing cell's instance as IEEE 1497 defines, so an
interconnect from ``a/Y`` to ``b/A`` inside instance ``u0`` joins ``u0/a/Y`` to
``u0/b/A``.  ``(INSTANCE *)`` names no concrete pins and is rejected.

Within one cell instance, delay entries resolve in file order by
{class}`ArcKey` without its mode: an ABSOLUTE entry starts an arc and each
later INCREMENT with the same key adds to the latest such arc.  Two ABSOLUTE
entries with one key stay two parallel arcs.

PORT and DEVICE delays are pin delays.  A PORT delay adds to every
INTERCONNECT arc entering its input pin and a DEVICE delay adds to every
IOPATH arc entering its output pin, or every IOPATH arc of the instance when
the DEVICE names no output.  Each conditional variant of a pin delay gives its
own parallel arc.  A PORT on a pin that no INTERCONNECT enters sits before
every path that starts at that pin, so it changes no path.  A DEVICE with no
IOPATH arc to add to raises, since SDF does not name the cell inputs it times.

On an acyclic cone, {func}`critical_path`, {func}`compute_slack` and
{func}`batch_endpoint_analysis` run one topological longest-path pass per
signal transition, in time linear in the cone.  A path delay composes
transition by transition, and the maximum over paths of the maximum over
transitions equals the maximum over transitions of the longest path, so RISE,
FALL and WORST are exact.  BEST maximises a minimum over transitions, which
has no longest-path form, and these functions reject it.  A cone with a
combinational loop, such as the IEEE 1497 example 1 design, has no
topological order, so these functions rank its simple paths exactly as
{func}`rank_paths` does, in time exponential in reconvergence.  With the
default ``traverse_registers``, a register whose output feeds back to its own
data input closes such a loop.  A graph where every pin lies on or after a
loop has no startpoint or no endpoint, and {func}`batch_endpoint_analysis`
raises {class}`CombinationalLoopError` for it.
{meth}`TimingGraph.find_paths`, {func}`rank_paths` and {func}`verify_path`
always enumerate every simple path.
"""

import functools
import operator
from dataclasses import dataclass, replace

import networkx as nx

from sdf_toolkit.core.model import (
    ArcKey,
    Corner,
    DelayMode,
    DelayValues,
    Device,
    EdgeSelect,
    Entry,
    EntryType,
    Interconnect,
    Iopath,
    MissingDelayError,
    Port,
    PortSpec,
    SDFFile,
    Setup,
    SetupHold,
    Transition,
)

ARC_ATTR = "arc"
"""Edge attribute of the backing graph that holds each arc's {class}`TimingEdge`."""

_X_TRANSITIONS = tuple(Transition)[6:]


class TimingGraphError(ValueError):
    """The SDF data cannot form a timing graph, or the graph cannot be analysed."""


class WildcardInstanceError(TimingGraphError):
    """A cell uses ``(INSTANCE *)``, which names no concrete pins."""


class IncrementWithoutBaseError(TimingGraphError):
    """An INCREMENT delay has no earlier ABSOLUTE delay for its arc."""


class UnplacedDeviceDelayError(TimingGraphError):
    """A DEVICE delay has no IOPATH arc to add to."""


class CombinationalLoopError(TimingGraphError):
    """Every pin lies on or after a loop, so no path has a start or an end.

    Attributes
    ----------
    cycle : tuple[str, ...]
        The pins of one cycle, in arc order.
    """

    def __init__(self, cycle: tuple[str, ...]) -> None:
        self.cycle = cycle
        loop = " -> ".join((*cycle, cycle[0]))
        super().__init__(
            f"Every pin of the timing graph lies on or after a combinational loop, "
            f"such as {loop}, so the graph has no startpoint or no endpoint. Pass "
            "sources and sinks explicitly, break the loop in the SDF, or build the "
            "graph with traverse_registers=False if the loop closes through a "
            "register's setup arc."
        )


@dataclass(frozen=True, slots=True)
class TimingEdge:
    """A directed timing arc between two pins.

    Attributes
    ----------
    source : str
        Fully-qualified source pin name (e.g., ``"B1/C1/i"``).
    sink : str
        Fully-qualified sink pin name (e.g., ``"B1/C1/z"``).
    delay : DelayValues
        Delay of the arc, including the PORT or DEVICE delay of its sink pin.
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
    """Prefix *pin* with *instance* and *divider*; a top-level pin stays as is."""
    if instance:
        return f"{instance}{divider}{pin}"
    return pin


def _resolve_delays(
    entries: list[Entry], *, instance: str
) -> list[Iopath | Interconnect | Port | Device]:
    """Fold each INCREMENT into the latest ABSOLUTE entry of its arc.

    Raises
    ------
    IncrementWithoutBaseError
        If an INCREMENT comes before any ABSOLUTE entry with its key.
    """
    resolved: list[Iopath | Interconnect | Port | Device] = []
    latest: dict[ArcKey, int] = {}
    for entry in entries:
        match entry:
            case Iopath() | Interconnect() | Port() | Device():
                key = replace(entry.key, mode=None)
            case _:
                continue
        match entry.mode:
            case DelayMode.ABSOLUTE:
                latest[key] = len(resolved)
                resolved.append(entry)
            case DelayMode.INCREMENT:
                if key not in latest:
                    msg = (
                        f"INCREMENT {key} in instance {instance!r} has no earlier "
                        "ABSOLUTE entry for the same arc to add to. Write the "
                        "ABSOLUTE delay before the INCREMENT."
                    )
                    raise IncrementWithoutBaseError(msg)
                base = resolved[latest[key]]
                resolved[latest[key]] = replace(base, delay=base.delay + entry.delay)
    return resolved


def _register_arc(
    entry: Setup | SetupHold, *, cell_type: str, instance: str, divider: str
) -> TimingEdge:
    """Return the data-to-clock arc of a setup check, timed by the setup limit.

    Hold adds no arc: it bounds the earliest arrival and is not a propagation
    cost, and its usually negative value would shorten path sums.
    """
    setup = entry.limit if isinstance(entry, Setup) else entry.setup
    return TimingEdge(
        source=_qualify_pin(instance, entry.data.port, divider),
        sink=_qualify_pin(instance, entry.reference.port, divider),
        delay=DelayValues.of(setup),
        entry_type=entry.type,
        cell_type=cell_type,
        instance=instance,
    )


def _require_node(graph: nx.MultiDiGraph, node: str) -> None:
    if node not in graph:
        raise nx.NodeNotFound(f"Node {node!r} not in graph")


def _require_path_ends(graph: nx.MultiDiGraph, ends: list[str]) -> None:
    """Raise CombinationalLoopError if a non-empty *graph* has no start or end pins.

    A finite graph where every pin has an arc in (or out) has a cycle.
    """
    if graph and not ends:
        raise CombinationalLoopError(cycle=tuple(u for u, *_ in nx.find_cycle(graph)))


class TimingGraph:
    """Directed multigraph of timing arcs built from an SDF file.

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

    Raises
    ------
    WildcardInstanceError
        If a cell uses ``(INSTANCE *)``.
    IncrementWithoutBaseError
        If an INCREMENT delay has no earlier ABSOLUTE delay for its arc.
    UnplacedDeviceDelayError
        If a DEVICE delay has no IOPATH arc to add to.
    """

    def __init__(self, sdf: SDFFile, traverse_registers: bool = True) -> None:
        self.divider = sdf.header.effective_divider
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        self._build(sdf, traverse_registers)

    def _build(self, sdf: SDFFile, traverse_registers: bool) -> None:
        """Add one edge per arc, after folding pin delays into the arcs."""
        divider = sdf.header.effective_divider
        arcs: list[TimingEdge] = []
        port_delays: dict[str, list[DelayValues]] = {}
        output_delays: dict[str, list[DelayValues]] = {}
        instance_delays: dict[str, list[DelayValues]] = {}

        for cell_type, instances in sdf.cells.items():
            for instance, entries in instances.items():
                if instance == "*":
                    msg = (
                        f"Cell {cell_type!r} uses (INSTANCE *), which matches every "
                        "instance of the cell type rather than naming one, so its "
                        "pins have no place in the timing graph. Expand the "
                        "wildcard into concrete INSTANCE paths first."
                    )
                    raise WildcardInstanceError(msg)
                pin = functools.partial(_qualify_pin, instance, divider=divider)
                for entry in _resolve_delays(entries, instance=instance):
                    match entry:
                        case Iopath():
                            source, sink = entry.input.port, entry.output.port
                        case Interconnect():
                            source, sink = entry.source.port, entry.load.port
                        case Port():
                            port_delays.setdefault(pin(entry.port.port), []).append(
                                entry.delay
                            )
                            continue
                        case Device(output=None):
                            instance_delays.setdefault(instance, []).append(entry.delay)
                            continue
                        case Device(output=PortSpec(port=output)):
                            output_delays.setdefault(pin(output), []).append(
                                entry.delay
                            )
                            continue
                    arcs.append(
                        TimingEdge(
                            source=pin(source),
                            sink=pin(sink),
                            delay=entry.delay,
                            entry_type=entry.type,
                            cell_type=cell_type,
                            instance=instance,
                        )
                    )
                if traverse_registers:
                    arcs.extend(
                        _register_arc(
                            entry,
                            cell_type=cell_type,
                            instance=instance,
                            divider=divider,
                        )
                        for entry in entries
                        if isinstance(entry, Setup | SetupHold)
                    )

        iopath_arcs = [a for a in arcs if a.entry_type == EntryType.IOPATH]
        for output in output_delays.keys() - {a.sink for a in iopath_arcs}:
            msg = (
                f"DEVICE delay on {output!r} has no IOPATH arc into that output "
                "to add to, and SDF does not name the cell inputs it times. Write "
                "IOPATH entries for the cell."
            )
            raise UnplacedDeviceDelayError(msg)
        for instance in instance_delays.keys() - {a.instance for a in iopath_arcs}:
            msg = (
                f"DEVICE delay of instance {instance!r} has no IOPATH arc to add "
                "to, and SDF does not name the cell inputs it times. Write IOPATH "
                "entries for the cell."
            )
            raise UnplacedDeviceDelayError(msg)

        for arc in arcs:
            match arc.entry_type:
                case EntryType.INTERCONNECT:
                    pin_delays = port_delays.get(arc.sink, [])
                case EntryType.IOPATH:
                    pin_delays = [
                        *output_delays.get(arc.sink, []),
                        *instance_delays.get(arc.instance, []),
                    ]
                case _:
                    pin_delays = []
            variants = [replace(arc, delay=arc.delay + d) for d in pin_delays]
            for variant in variants or [arc]:
                self._graph.add_edge(
                    variant.source, variant.sink, **{ARC_ATTR: variant}
                )

    @property
    def graph(self) -> nx.MultiDiGraph:
        """The backing graph; each edge holds its TimingEdge under ARC_ATTR."""
        return self._graph

    def nodes(self) -> set[str]:
        """Return all fully-qualified pin names in the graph."""
        return set(self._graph.nodes())

    def startpoints(self) -> set[str]:
        """Return the pins no arc enters."""
        return {n for n, d in self._graph.in_degree() if d == 0}

    def endpoints(self) -> set[str]:
        """Return the pins no arc leaves."""
        return {n for n, d in self._graph.out_degree() if d == 0}

    def edges(self) -> list[TimingEdge]:
        """Return every arc in the graph."""
        return [arc for _, _, arc in self._graph.edges(data=ARC_ATTR)]

    def successors(self, node: str) -> list[TimingEdge]:
        """Return the arcs leaving *node*."""
        return [arc for _, _, arc in self._graph.out_edges(node, data=ARC_ATTR)]

    def predecessors(self, node: str) -> list[TimingEdge]:
        """Return the arcs entering *node*."""
        return [arc for _, _, arc in self._graph.in_edges(node, data=ARC_ATTR)]

    def find_paths(self, source: str, sink: str) -> list[list[TimingEdge]]:
        """Return every simple path from *source* to *sink* as its arcs.

        Each choice among parallel arcs is its own path.  The count grows
        exponentially with reconvergence; use {func}`critical_path` for the
        slowest path of a large cone.

        Raises
        ------
        networkx.NodeNotFound
            If *source* or *sink* is not a pin of the graph.
        """
        _require_node(self._graph, source)
        _require_node(self._graph, sink)
        if source == sink:
            return []
        return [
            [self._graph.edges[u, v, k][ARC_ATTR] for u, v, k in path]
            for path in nx.all_simple_edge_paths(self._graph, source, sink)
        ]

    def compose_delay(self, path: list[TimingEdge]) -> DelayValues:
        """Sum the delays along *path* transition by transition.

        Raises
        ------
        ValueError
            If the path is empty.
        """
        if not path:
            raise ValueError("Cannot compose delay for an empty path.")

        return functools.reduce(operator.add, (edge.delay for edge in path))

    def compose(self, source: str, sink: str) -> list[DelayValues]:
        """Return the composed delay of every path from *source* to *sink*."""
        return [self.compose_delay(p) for p in self.find_paths(source, sink)]


def verify_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    expected: DelayValues,
    tolerance: float = 1e-9,
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

    def _sort_key(rp: RankedPath) -> tuple[int, float]:
        if rp.scalar is None:
            # (1, ...) sorts after every (0, ...) whichever direction is asked.
            return (1, 0.0)
        sign = -1.0 if descending else 1.0
        return (0, sign * rp.scalar)

    ranked.sort(key=_sort_key)
    return ranked


def _transitions(edge: EdgeSelect) -> tuple[Transition, ...]:
    """Transitions whose longest paths together give the *edge* maximum.

    Raises
    ------
    ValueError
        For BEST, which has no longest-path form.
    """
    match edge:
        case EdgeSelect.RISE:
            return (Transition.T01,)
        case EdgeSelect.FALL:
            return (Transition.T10,)
        case EdgeSelect.WORST:
            return tuple(Transition)
        case EdgeSelect.BEST:
            msg = (
                "EdgeSelect.BEST takes the smallest transition of each path, and "
                "the path maximising that minimum has no longest-path form. Use "
                "rank_paths, which enumerates every path, to rank by BEST."
            )
            raise ValueError(msg)


def _transition_delay(
    delay: DelayValues, transition: Transition, corner: Corner
) -> float | None:
    """Value of *delay* for *transition* at *corner*, None where the file has none."""
    if transition in _X_TRANSITIONS and len(delay.rvalues) != 12:
        return None
    return delay.for_transition(transition).at(corner)


# Pin -> (latest arrival, arc it arrives through); the source has no arc.
_Arrivals = dict[str, tuple[float, TimingEdge | None]]


def _longest_arrivals(
    graph: nx.MultiDiGraph,
    *,
    source: str,
    order: list[str],
    transition: Transition,
    corner: Corner,
) -> _Arrivals:
    """Relax arcs in topological *order* from *source* to get latest arrivals.

    An arc without a value for *transition* at *corner* carries no arrival,
    as composing it would leave that transition without a value.
    """
    best: _Arrivals = {source: (0.0, None)}
    for node in order:
        if node not in best:
            continue
        arrival = best[node][0]
        for _, succ, arc in graph.out_edges(node, data=ARC_ATTR):
            weight = _transition_delay(arc.delay, transition, corner)
            if weight is None:
                continue
            candidate = arrival + weight
            if succ not in best or candidate > best[succ][0]:
                best[succ] = (candidate, arc)
    return best


def _path_counts(
    graph: nx.MultiDiGraph, *, source: str, order: list[str]
) -> dict[str, int]:
    """Count the paths from *source* to each pin, parallel arcs separately."""
    counts = {source: 1}
    for node in order:
        if node not in counts:
            continue
        for _, succ in graph.out_edges(node):
            counts[succ] = counts.get(succ, 0) + counts[node]
    return counts


def _backtrack(arrivals: _Arrivals, sink: str) -> list[TimingEdge]:
    edges: list[TimingEdge] = []
    while (arc := arrivals[sink][1]) is not None:
        edges.append(arc)
        sink = arc.source
    edges.reverse()
    return edges


def _longest_path(
    graph: TimingGraph, source: str, sink: str, *, corner: Corner, edge: EdgeSelect
) -> tuple[float, list[TimingEdge]] | None:
    """Delay and arcs of the slowest path with a value, or None if there is none."""
    transitions = _transitions(edge)
    g = graph.graph
    _require_node(g, source)
    _require_node(g, sink)
    downstream = nx.descendants(g, source)
    if sink not in downstream:
        return None
    cone = g.subgraph((downstream | {source}) & (nx.ancestors(g, sink) | {sink}))
    if not nx.is_directed_acyclic_graph(cone):
        slowest = rank_paths(graph, source, sink, corner=corner, edge=edge)[0]
        return None if slowest.scalar is None else (slowest.scalar, slowest.edges)
    order = list(nx.topological_sort(cone))
    found: tuple[float, list[TimingEdge]] | None = None
    for transition in transitions:
        arrivals = _longest_arrivals(
            g, source=source, order=order, transition=transition, corner=corner
        )
        if sink in arrivals and (found is None or arrivals[sink][0] > found[0]):
            found = (arrivals[sink][0], _backtrack(arrivals, sink))
    return found


def critical_path(
    graph: TimingGraph,
    source: str,
    sink: str,
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> RankedPath | None:
    """Return the path with the largest scalar delay.

    An acyclic cone takes one longest-path pass per transition; a cone with a
    loop ranks its simple paths.

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
        The critical (slowest) path, or None if no path from *source* to
        *sink* has a value at *corner*.

    Raises
    ------
    ValueError
        If *edge* is BEST.

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
    4.5
    """
    found = _longest_path(graph, source, sink, corner=corner, edge=edge)
    if found is None:
        return None
    scalar, edges = found
    return RankedPath(edges=edges, delay=graph.compose_delay(edges), scalar=scalar)


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
        The slack, or None if no path from *source* to *sink* has a value at
        *corner*.

    Raises
    ------
    ValueError
        If *edge* is BEST.

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
    ...     .add_cell("TOP", "")
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
    found = _longest_path(graph, source, sink, corner=corner, edge=edge)
    return None if found is None else period - found[0]


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
        The scalar delay of the critical path, or None if no path has a value.
    path_count : int
        Number of paths between source and sink, parallel arcs counted apart.
    """

    source: str
    sink: str
    critical_delay: float | None
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
            _longest_arrivals(
                graph, source=src, order=downstream, transition=t, corner=corner
            )
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


def _batch_ranked(
    graph: TimingGraph,
    *,
    sources: list[str],
    sinks: list[str],
    corner: Corner,
    edge: EdgeSelect,
) -> list[EndpointResult]:
    """Rank the simple paths of every pair, for a graph with a loop."""
    results: list[EndpointResult] = []
    for src in sources:
        for snk in sinks:
            ranked = rank_paths(graph, src, snk, corner=corner, edge=edge)
            if ranked:
                results.append(
                    EndpointResult(
                        source=src,
                        sink=snk,
                        critical_delay=ranked[0].scalar,
                        path_count=len(ranked),
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

    An acyclic graph takes one longest-path pass per source and transition;
    a graph with a loop ranks the simple paths of each pair.

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
    ValueError
        If *edge* is BEST.
    CombinationalLoopError
        If *sources* or *sinks* is not given and every pin lies on or after a
        loop, so the graph has no startpoint or no endpoint.
    networkx.NodeNotFound
        If a given source or sink is not a pin of the graph.

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
    4.5
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
        results = _batch_ranked(
            graph, sources=sources, sinks=sinks, corner=corner, edge=edge
        )

    def _sort_key(r: EndpointResult) -> tuple[int, float]:
        if r.critical_delay is None:
            return (1, 0.0)
        return (0, -r.critical_delay)

    results.sort(key=_sort_key)
    return results


def decompose_delay(total: DelayValues, known: DelayValues) -> DelayValues:
    """Return the unknown delay segment, ``total - known``."""
    return total - known
