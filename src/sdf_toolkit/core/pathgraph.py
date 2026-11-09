"""Timing graph of an SDF file and longest-path analysis over it.

Nodes are pins named by instance path, the file's divider and port.  Each
IOPATH, INTERCONNECT and (with ``traverse_registers``) setup check becomes one
arc of a ``networkx.MultiDiGraph``, and the arc's {class}`TimingEdge` is the
only attribute stored on it, under {data}`ARC_ATTR`.  INTERCONNECT ports are
scoped to the enclosing cell's instance as IEEE 1497 defines, so an
interconnect from ``a/Y`` to ``b/A`` inside instance ``u0`` joins ``u0/a/Y`` to
``u0/b/A``.  ``(INSTANCE *)`` names no concrete pins and is rejected.

Delay entries apply in the order {attr}`SDFFile.cells` holds them, with
ABSOLUTE and INCREMENT meaning what IEEE 1497 gives them.  That order is file
order within one cell instance, but the parser groups cells by cell type, so
across cell types it follows the first block of each type.  An IOPATH or
INTERCONNECT arc is identified by its {class}`ArcKey` over qualified pins
without the mode.  An ABSOLUTE entry sets the arc's delay, replacing any
earlier one, and an INCREMENT adds to the delay the arc holds at that point.
A setup check arc has the {class}`ArcKey` of its check over qualified pins,
and a later check with the same key replaces the earlier one.  Arcs with
different keys, such as edge or COND variants, stay parallel.

A PORT entry acts on every INTERCONNECT arc entering its pin and a DEVICE
entry on every IOPATH arc into its output, or every IOPATH arc of the
instance when the DEVICE names no output.  ABSOLUTE replaces the delay of
those arcs and INCREMENT adds to it, on the arcs that exist when the entry
applies; a later ABSOLUTE INTERCONNECT or IOPATH entry replaces the result
again.  A PORT on a pin that no INTERCONNECT enters sits before every path
that starts at that pin, so it changes no path.  A DEVICE with no IOPATH arc
raises, since SDF does not name the cell inputs it times, and a PORT or DEVICE
under COND raises, since IEEE 1497 conditions only IOPATH delays.

On an acyclic cone, {func}`critical_path`, {func}`compute_slack` and
{func}`batch_endpoint_analysis` run one topological longest-path pass per
signal transition, in time linear in the cone.  A path delay composes
transition by transition, and the maximum over paths of the maximum over
transitions equals the maximum over transitions of the longest path, so RISE,
FALL and WORST are exact.  BEST maximises a minimum over transitions, which
has no longest-path form, and these functions reject it.  A cone with a
loop, such as the IEEE 1497 example 1 design, raises
{class}`TimingLoopError` naming one cycle, since the longest simple path of a
graph with a cycle is NP-hard and these functions do not approximate it.
With the default ``traverse_registers``, a register whose output feeds back to
its own data input closes such a loop.  A search through a loop needs a
source and sink whose cone avoids it.

{meth}`TimingGraph.startpoints` and {meth}`TimingGraph.endpoints` add the
clock and data pins of every register, a cell with a SETUP or SETUPHOLD check,
to the pins no arc enters or leaves.  A register clock pin launches a path and
its data pin captures one whether or not ``traverse_registers`` adds the arc
between them, so register feedback leaves the default ends of
{func}`batch_endpoint_analysis` non-empty.

{meth}`TimingGraph.find_paths`, {func}`rank_paths` and {func}`verify_path`
always enumerate every simple path, loops included.

A register is an instance with a SETUP, HOLD or SETUPHOLD check, and every
pin such a check names is a pin of the graph.
{meth}`TimingGraph.register_inputs` lists each register data pin with its
clock pin and the limits of all its checks, {meth}`TimingGraph.combinational`
drops the arcs that cross a register, and
{meth}`TimingGraph.register_input_timing` turns the worst check into the
setup and hold requirement at a pin that feeds the register.
{func}`critical_path` and {func}`fastest_path` take a pin to pass through,
which gives clock-to-output delays through a register's clock pin.

The structural searches, {meth}`TimingGraph.within`,
{meth}`TimingGraph.nearest_path` and {meth}`TimingGraph.convergence_pin`,
break ties by the order the graph holds its pins and arcs, which follows the
entries, or by pin name, never by the order of a Python set, so their results
do not depend on ``PYTHONHASHSEED``.
"""

import copy
import functools
import operator
import weakref
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from itertools import pairwise
from typing import assert_never

import networkx as nx

from sdf_toolkit.core.identifiers import sdf_unescape, split_path
from sdf_toolkit.core.model import (
    DEFAULT_TOLERANCE,
    ArcKey,
    Arrival,
    BidirectSkew,
    Corner,
    DelayMode,
    DelayValues,
    Departure,
    Device,
    DiffConstraint,
    EdgeSelect,
    EntryType,
    Hold,
    Interconnect,
    Iopath,
    Label,
    MissingDelayError,
    NetDelay,
    NoChange,
    Number,
    PathConstraint,
    PathPulse,
    PathPulsePercent,
    Period,
    PeriodConstraint,
    Port,
    Recovery,
    Recrem,
    Removal,
    SDFFile,
    Setup,
    SetupHold,
    Skew,
    SkewConstraint,
    Slack,
    SumConstraint,
    Transition,
    Values,
    Waveform,
    Width,
    to_decimal,
)
from sdf_toolkit.errors import SDFToolkitError

ARC_ATTR = "arc"
"""Edge attribute of the backing graph that holds each arc's {class}`TimingEdge`."""

_X_TRANSITIONS = tuple(Transition)[6:]


class TimingGraphError(SDFToolkitError, ValueError):
    """The SDF data cannot form a timing graph, or the graph cannot be analysed."""


class WildcardInstanceError(TimingGraphError):
    """A cell uses ``(INSTANCE *)``, which names no concrete pins."""


class IncrementWithoutBaseError(TimingGraphError):
    """An INCREMENT delay has no earlier delay for its arc to add to."""


class UnplacedDeviceDelayError(TimingGraphError):
    """A DEVICE delay has no IOPATH arc to apply to."""


class ConditionalPinDelayError(TimingGraphError):
    """A PORT or DEVICE delay carries a COND, which IEEE 1497 gives only IOPATH."""


class NetDelayError(TimingGraphError):
    """A NETDELAY names a net, and the graph has no net to find its loads by."""


class TimingLoopError(TimingGraphError):
    """A longest-path search met a loop, which it does not support.

    Attributes
    ----------
    cycle : tuple[str, ...]
        The pins of one cycle, in arc order.
    source : str | None
        The source of the search, or None when the whole graph has no
        startpoint or no endpoint.
    sink : str | None
        The sink of the search, or None as for *source*.
    """

    def __init__(
        self,
        cycle: tuple[str, ...],
        *,
        source: str | None = None,
        sink: str | None = None,
    ) -> None:
        self.cycle = cycle
        self.source = source
        self.sink = sink
        loop = " -> ".join((*cycle, cycle[0]))
        where = (
            "every pin of the timing graph lies on or after it, so the graph has "
            "no startpoint or no endpoint"
            if source is None
            else f"the paths from {source} to {sink} pass through it"
        )
        super().__init__(
            f"Longest-path search does not support loops, and {where}: {loop}. "
            "Choose a source and sink whose paths avoid the loop, break the loop "
            "in the SDF, or build the graph with traverse_registers=False if the "
            "loop closes through a register's setup arc."
        )


def _cycle(graph: nx.MultiDiGraph) -> tuple[str, ...]:
    """Return the pins of one cycle of *graph*, in arc order."""
    return tuple(u for u, *_ in nx.find_cycle(graph))


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
        Delay of the arc after every entry that sets or adds to it.
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


def _qualify_pin(instance: str, pin: str, divider: str) -> str:
    """Prefix *pin* with *instance* and *divider*; a top-level pin stays as is."""
    if instance:
        return f"{instance}{divider}{pin}"
    return pin


@dataclass
class _ArcTable:
    """IOPATH and INTERCONNECT arcs, keyed by ArcKey over qualified pins."""

    arcs: dict[ArcKey, TimingEdge] = field(default_factory=dict)
    into: dict[tuple[EntryType, str], list[ArcKey]] = field(default_factory=dict)
    iopaths_of: dict[str, list[ArcKey]] = field(default_factory=dict)

    def put(self, key: ArcKey, arc: TimingEdge, mode: DelayMode) -> None:
        """Set (ABSOLUTE) or add to (INCREMENT) the delay of the arc *key*.

        Raises
        ------
        IncrementWithoutBaseError
            If an INCREMENT comes before any ABSOLUTE entry with its key.
        """
        if key not in self.arcs:
            if mode is DelayMode.INCREMENT:
                msg = (
                    f"INCREMENT {key} has no earlier ABSOLUTE entry for the same "
                    "arc to add to. Write the ABSOLUTE delay before the INCREMENT."
                )
                raise IncrementWithoutBaseError(msg)
            self.into.setdefault((arc.entry_type, arc.sink), []).append(key)
            if arc.entry_type is EntryType.IOPATH:
                self.iopaths_of.setdefault(arc.instance, []).append(key)
            self.arcs[key] = arc
            return
        self.apply([key], delay=arc.delay, mode=mode)

    def apply(self, keys: list[ArcKey], *, delay: DelayValues, mode: DelayMode) -> None:
        """Replace (ABSOLUTE) or add *delay* to (INCREMENT) the delay of *keys*."""
        for key in keys:
            arc = self.arcs[key]
            match mode:
                case DelayMode.ABSOLUTE:
                    self.arcs[key] = replace(arc, delay=delay)
                case DelayMode.INCREMENT:
                    self.arcs[key] = replace(arc, delay=arc.delay + delay)


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


_Limits = tuple[Values | None, Values | None]


def _check_limits(entry: Setup | Hold | SetupHold) -> _Limits:
    """Return the setup and hold limit of a check, None for a kind it lacks."""
    match entry:
        case Setup():
            return (entry.limit, None)
        case Hold():
            return (None, entry.limit)
        case SetupHold():
            return (entry.setup, entry.hold)


@dataclass(frozen=True, slots=True)
class RegisterInput:
    """The setup and hold checks between a register data pin and its clock pin.

    A register is an instance with a SETUP, HOLD or SETUPHOLD check.  Every
    such check between the same two pins, whatever its edges or conditions,
    belongs to one RegisterInput; a later check with the same arc key
    replaces the earlier one, as it does for the arcs ``traverse_registers``
    adds.

    Attributes
    ----------
    data : str
        Qualified data pin, the first port of the checks.
    clock : str
        Qualified clock pin, the reference port of the checks.
    setup : tuple[Values, ...]
        The limit of each SETUP check and the setup limit of each SETUPHOLD,
        in file order.
    hold : tuple[Values, ...]
        The limit of each HOLD check and the hold limit of each SETUPHOLD,
        in file order.
    """

    data: str
    clock: str
    setup: tuple[Values, ...]
    hold: tuple[Values, ...]


@dataclass(frozen=True, slots=True)
class RegisterInputTiming:
    """The setup and hold requirement a pin that feeds a register input meets.

    Attributes
    ----------
    setup : Decimal | None
        Latest data arrival plus the largest setup limit, less the earliest
        clock arrival, or None if a term has no value.
    hold : Decimal | None
        Largest hold limit plus the latest clock arrival, less the earliest
        data arrival, or None if a term has no value.
    """

    setup: Decimal | None
    hold: Decimal | None


def _largest(limits: tuple[Values, ...], corner: Corner) -> Decimal | None:
    """Return the largest of *limits* at *corner*, or None without a value."""
    return max(
        (value for limit in limits if (value := limit.at(corner)) is not None),
        default=None,
    )


def _device_arcs(
    table: _ArcTable, *, instance: str, output: str | None
) -> list[ArcKey]:
    """IOPATH arcs a DEVICE acts on: those into *output*, else all of *instance*."""
    if output is None:
        return table.iopaths_of.get(instance, [])
    return table.into.get((EntryType.IOPATH, output), [])


def _require_node(graph: nx.MultiDiGraph, node: str) -> None:
    if node not in graph:
        raise nx.NodeNotFound(f"Node {node!r} not in graph")


def _require_path_ends(graph: nx.MultiDiGraph, ends: list[str]) -> None:
    """Raise TimingLoopError if a non-empty *graph* has no start or end pins.

    A finite graph where every pin has an arc in (or out) has a cycle.
    """
    if graph and not ends:
        raise TimingLoopError(cycle=_cycle(graph))


class TimingGraph:
    """Directed multigraph of timing arcs built from an SDF file.

    Parameters
    ----------
    sdf : SDFFile
        The parsed SDF file to build the graph from.
    traverse_registers : bool, optional
        When True, the SETUP/SETUPHOLD timing checks add one data-pin to
        clock-pin edge per check key, whose delay is the setup time of the
        last check with that key.
        Sequential cells then become traversable: a path can continue
        from a register's data input through its clock pin and on through
        the cell's clock-to-output IOPATH. By default True, which makes
        registers traversable so timing paths may cross sequential cells.
        A register whose output feeds back to its data input then closes a
        loop, which the longest-path functions reject.

    Raises
    ------
    WildcardInstanceError
        If a cell uses ``(INSTANCE *)``.
    IncrementWithoutBaseError
        If an INCREMENT delay has no earlier delay for its arc to add to.
    UnplacedDeviceDelayError
        If a DEVICE delay has no IOPATH arc to apply to.
    ConditionalPinDelayError
        If a PORT or DEVICE delay carries a COND.
    NetDelayError
        If the file has a NETDELAY entry.
    """

    def __init__(self, sdf: SDFFile, traverse_registers: bool = True) -> None:
        self.divider = sdf.header.effective_divider
        self._graph: nx.MultiDiGraph = nx.MultiDiGraph()
        # Shared with the graphs within() derives, so never changed after _build.
        self._register_clocks: set[str] = set()
        self._register_data: set[str] = set()
        self._register_instances: frozenset[str] = frozenset()
        self._register_inputs: tuple[RegisterInput, ...] = ()
        # Every arc in the order it entered the graph, which a derived graph
        # replays so each pin keeps the order of its arcs both ways.
        self._arcs: tuple[TimingEdge, ...] = ()
        # The graphs register_input_timing searches, built on first use.
        self._data_graph: nx.MultiDiGraph | None = None
        self._clock_graph: nx.MultiDiGraph | None = None
        self._build(sdf, traverse_registers)

    def _build(self, sdf: SDFFile, traverse_registers: bool) -> None:
        """Apply every delay entry in order, then add one edge per arc."""
        divider = sdf.header.effective_divider
        table = _ArcTable()
        register_arcs: dict[ArcKey, TimingEdge] = {}
        # (data, clock) -> check key -> (setup, hold) limits; a later check
        # with the same key replaces the earlier one in place.
        checks: dict[tuple[str, str], dict[ArcKey, _Limits]] = {}
        register_instances: set[str] = set()
        absolute_ports: set[str] = set()
        devices: list[tuple[str, str | None]] = []

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
                for entry in entries:
                    match entry:
                        case Iopath() | Interconnect():
                            source, sink = (
                                replace(p, port=pin(p.port)) for p in entry.pins
                            )
                            arc = TimingEdge(
                                source=source.port,
                                sink=sink.port,
                                delay=entry.delay,
                                entry_type=entry.type,
                                cell_type=cell_type,
                                instance=instance,
                            )
                            key = ArcKey(
                                type=entry.type,
                                pins=(source, sink),
                                cond=entry.cond,
                                mode=None,
                            )
                            table.put(key, arc, entry.mode)
                        case Port() | Device() if entry.cond is not None:
                            msg = (
                                f"{entry.key} in instance {instance!r} carries a "
                                "COND, but IEEE 1497 conditions only IOPATH delays, "
                                "so the arcs it would act on are undefined. Remove "
                                "the COND or write the delay as an IOPATH."
                            )
                            raise ConditionalPinDelayError(msg)
                        case Port():
                            target = pin(entry.port.port)
                            keys = table.into.get((EntryType.INTERCONNECT, target), [])
                            if entry.mode is DelayMode.ABSOLUTE:
                                absolute_ports.add(target)
                            elif not keys and target not in absolute_ports:
                                msg = (
                                    f"INCREMENT PORT {target!r} has no INTERCONNECT "
                                    "arc into the pin and no earlier ABSOLUTE PORT "
                                    "to add to. Write the ABSOLUTE delay first."
                                )
                                raise IncrementWithoutBaseError(msg)
                            table.apply(keys, delay=entry.delay, mode=entry.mode)
                        case Device():
                            output = (
                                None if entry.output is None else pin(entry.output.port)
                            )
                            keys = _device_arcs(table, instance=instance, output=output)
                            if entry.mode is DelayMode.INCREMENT and not keys:
                                msg = (
                                    f"INCREMENT DEVICE in instance {instance!r} has "
                                    "no earlier IOPATH arc to add to. Write the "
                                    "IOPATH entries before the DEVICE."
                                )
                                raise IncrementWithoutBaseError(msg)
                            table.apply(keys, delay=entry.delay, mode=entry.mode)
                            devices.append((instance, output))
                        case NetDelay():
                            msg = (
                                f"{entry.key} in instance {instance!r} times every "
                                "source to every load of its net, but the graph "
                                "holds pins and arcs, not nets. Write the net as "
                                "INTERCONNECT entries, one per load."
                            )
                            raise NetDelayError(msg)
                        case PathPulse() | PathPulsePercent():
                            # Pulse limits filter glitches; the arc delay stays.
                            pass
                        case Setup() | Hold() | SetupHold():
                            key = replace(
                                entry.key,
                                pins=tuple(
                                    replace(p, port=pin(p.port)) for p in entry.pins
                                ),
                            )
                            register_instances.add(instance)
                            pair = (pin(entry.data.port), pin(entry.reference.port))
                            checks.setdefault(pair, {})[key] = _check_limits(entry)
                            if isinstance(entry, Hold):
                                continue
                            arc = _register_arc(
                                entry,
                                cell_type=cell_type,
                                instance=instance,
                                divider=divider,
                            )
                            self._register_data.add(arc.source)
                            self._register_clocks.add(arc.sink)
                            if traverse_registers:
                                register_arcs[key] = arc
                        case (
                            Recovery()
                            | Removal()
                            | Recrem()
                            | Skew()
                            | BidirectSkew()
                            | NoChange()
                            | Width()
                            | Period()
                        ):
                            # A check bounds when a signal may change; it times no arc.
                            pass
                        case (
                            PathConstraint()
                            | SumConstraint()
                            | DiffConstraint()
                            | PeriodConstraint()
                            | SkewConstraint()
                            | Arrival()
                            | Departure()
                            | Slack()
                            | Waveform()
                        ):
                            # A constraint is a goal for the path, not an arc on it.
                            pass
                        case Label():
                            # A LABEL sets a model variable that no SDF arc names.
                            pass
                        case _:
                            assert_never(entry)

        for instance, output in devices:
            if not _device_arcs(table, instance=instance, output=output):
                where = f"output {output!r}" if output else f"instance {instance!r}"
                msg = (
                    f"DEVICE delay of {where} has no IOPATH arc to apply to, and "
                    "SDF does not name the cell inputs it times. Write IOPATH "
                    "entries for the cell."
                )
                raise UnplacedDeviceDelayError(msg)

        self._arcs = (*table.arcs.values(), *register_arcs.values())
        for arc in self._arcs:
            self._graph.add_edge(arc.source, arc.sink, **{ARC_ATTR: arc})
        # Every pin a register check names is a pin of the graph, with or
        # without the arcs traverse_registers adds; adding them last keeps
        # the order of the pins the arcs bring in.
        for data, clock in checks:
            self._graph.add_nodes_from((data, clock))
        self._register_instances = frozenset(register_instances)
        self._register_inputs = tuple(
            RegisterInput(
                data=data,
                clock=clock,
                setup=tuple(setup for setup, _ in limits.values() if setup is not None),
                hold=tuple(hold for _, hold in limits.values() if hold is not None),
            )
            for (data, clock), limits in checks.items()
        )
        nx.freeze(self._graph)

    @property
    def graph(self) -> nx.MultiDiGraph:
        """The backing graph; each edge holds its TimingEdge under ARC_ATTR.

        The graph is frozen: networkx raises on any change to it, so the
        searches can keep what they learn about it.
        """
        return self._graph

    def nodes(self) -> set[str]:
        """Return all fully-qualified pin names in the graph."""
        return set(self._graph.nodes())

    def startpoints(self) -> set[str]:
        """Return the pins no arc enters and the register clock pins in the graph."""
        unentered = {n for n, d in self._graph.in_degree() if d == 0}
        return unentered | (self._register_clocks & self._graph.nodes)

    def endpoints(self) -> set[str]:
        """Return the pins no arc leaves and the register data pins in the graph."""
        unleft = {n for n, d in self._graph.out_degree() if d == 0}
        return unleft | (self._register_data & self._graph.nodes)

    def primary_inputs(self) -> list[str]:
        """Return the top-level pins no arc enters, in graph order.

        A top-level pin has no instance path, so its name holds no unescaped
        divider.
        """
        return [
            pin
            for pin, degree in self._graph.in_degree()
            if degree == 0 and self._is_top_level(pin)
        ]

    def primary_outputs(self) -> list[str]:
        """Return the top-level pins no arc leaves, in graph order."""
        return [
            pin
            for pin, degree in self._graph.out_degree()
            if degree == 0 and self._is_top_level(pin)
        ]

    def _is_top_level(self, pin: str) -> bool:
        return len(split_path(pin, self.divider)) == 1

    def within(self, instance: str) -> "TimingGraph":
        r"""Return a copy that keeps only the pins inside *instance*.

        A pin is inside *instance* when its name starts with *instance* and
        the divider, so ``within("u0")`` keeps ``u0/a/Y`` but not ``u01/Y``,
        and *instance* is written as in the graph, escapes included.  The
        copy keeps every arc between two kept pins, so its searches, forward
        and reverse, never leave *instance*.  Pins and arcs keep the order of
        this graph, so every search on the copy breaks ties as it would here.
        This graph is unchanged.

        Raises
        ------
        TimingGraphError
            If *instance* is empty or ends in an unescaped divider.
        SDFNameError
            If *instance* ends in a backslash that escapes nothing.

        Examples
        --------
        >>> from sdf_toolkit.parser.parser import parse_sdf
        >>> sdf = parse_sdf(
        ...     '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "TOP") (INSTANCE)'
        ...     " (DELAY (ABSOLUTE (INTERCONNECT u0/a/Y u0/b/A (1))"
        ...     " (INTERCONNECT u0/b/A u01/c/A (1))))))"
        ... )
        >>> sorted(TimingGraph(sdf).within("u0").nodes())
        ['u0/a/Y', 'u0/b/A']
        """
        sdf_unescape(instance)
        if not instance or split_path(instance, self.divider)[-1] == "":
            msg = (
                f"within() takes an instance path such as 'u0{self.divider}u1', "
                f"got {instance!r}. Pass the path without a trailing divider."
            )
            raise TimingGraphError(msg)
        prefix = f"{instance}{self.divider}"
        inside = tuple(
            register
            for register in self._register_inputs
            if register.data.startswith(prefix) and register.clock.startswith(prefix)
        )
        return self._derived(
            pins=lambda pin: pin.startswith(prefix), register_inputs=inside
        )

    def nearest_path(
        self, source: str, targets: Iterable[str], *, reverse: bool = False
    ) -> list[str] | None:
        """Return the fewest-arc path from *source* to the nearest of *targets*.

        The search is breadth first and visits the neighbours of each pin in
        the order the graph holds its arcs, which is the order of the
        entries (see the module notes).  The first target it reaches wins, so
        of several targets the same number of arcs away the one reached
        through the earliest arcs is chosen, along the path that reached it.
        With *reverse* the search follows arcs backwards, towards the pins
        that drive *source*.

        Parameters
        ----------
        source : str
            The pin to start from.
        targets : Iterable[str]
            The pins the path may end at.
        reverse : bool, optional
            Search against the arcs, by default False.

        Returns
        -------
        list[str] | None
            The pins of the path, *source* first and the target last; just
            ``[source]`` when *source* is a target.  None if no target can
            be reached.

        Raises
        ------
        TimingGraphError
            If *targets* is empty.
        networkx.NodeNotFound
            If *source* or a target is not a pin of the graph.

        Examples
        --------
        >>> from sdf_toolkit.parser.parser import parse_sdf
        >>> sdf = parse_sdf(
        ...     '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "TOP") (INSTANCE)'
        ...     " (DELAY (ABSOLUTE (INTERCONNECT a b (1)) (INTERCONNECT b c (1))"
        ...     " (INTERCONNECT a d (5))))))"
        ... )
        >>> g = TimingGraph(sdf)
        >>> g.nearest_path("a", ["c", "d"])
        ['a', 'd']
        >>> g.nearest_path("c", ["a"], reverse=True)
        ['c', 'b', 'a']
        """
        _require_node(self._graph, source)
        goal = set()
        for target in targets:
            _require_node(self._graph, target)
            goal.add(target)
        if not goal:
            msg = "nearest_path() needs at least one target pin"
            raise TimingGraphError(msg)
        neighbours = self._graph.predecessors if reverse else self._graph.successors
        parent: dict[str, str | None] = {source: None}
        queue = deque([source])
        found = source if source in goal else None
        while found is None and queue:
            pin = queue.popleft()
            for neighbour in neighbours(pin):
                if neighbour in parent:
                    continue
                parent[neighbour] = pin
                if neighbour in goal:
                    found = neighbour
                    break
                queue.append(neighbour)
        if found is None:
            return None
        path = [found]
        while (previous := parent[path[-1]]) is not None:
            path.append(previous)
        path.reverse()
        return path

    def convergence_pin(self, sources: Iterable[str]) -> str | None:
        """Return the earliest pin that every one of *sources* reaches.

        A pin reached from every source, counting each source as reaching
        itself, is common.  Of the common pins, the candidates are those of
        the strongly connected components of the common pins that no arc
        enters from another such component, the earliest common region.
        The chosen candidate has the fewest arcs on the longest of its
        fewest-arc paths from the sources.  Ties go to the candidate that
        reaches the most pins, then to the smallest pin name, so the choice
        depends on neither the order of *sources* nor the hash seed.  A
        single source is its own convergence pin.

        Parameters
        ----------
        sources : Iterable[str]
            The pins that converge; repeats count once.

        Returns
        -------
        str | None
            The convergence pin, or None if no pin is reached from every
            source.

        Raises
        ------
        TimingGraphError
            If *sources* is empty.
        networkx.NodeNotFound
            If a source is not a pin of the graph.

        Examples
        --------
        >>> from sdf_toolkit.parser.parser import parse_sdf
        >>> sdf = parse_sdf(
        ...     '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "TOP") (INSTANCE)'
        ...     " (DELAY (ABSOLUTE (INTERCONNECT a m (1)) (INTERCONNECT b m (1))"
        ...     " (INTERCONNECT m y (1))))))"
        ... )
        >>> TimingGraph(sdf).convergence_pin(["a", "b"])
        'm'
        """
        unique = list(dict.fromkeys(sources))
        if not unique:
            msg = "convergence_pin() needs at least one source pin"
            raise TimingGraphError(msg)
        for source in unique:
            _require_node(self._graph, source)
        hops = [nx.single_source_shortest_path_length(self._graph, s) for s in unique]
        common = set(hops[0]).intersection(*hops[1:])
        if not common:
            return None
        region = nx.subgraph_view(self._graph, filter_node=common.__contains__)
        component = {
            pin: index
            for index, members in enumerate(nx.strongly_connected_components(region))
            for pin in members
        }
        entered = {
            component[v] for u, v in region.edges() if component[u] != component[v]
        }
        candidates = [pin for pin in region if component[pin] not in entered]
        cost = {pin: max(h[pin] for h in hops) for pin in candidates}
        fewest = min(cost.values())
        tied = [pin for pin in candidates if cost[pin] == fewest]
        if len(tied) > 1:
            # Every pin a common pin reaches is common, so this is its reach in
            # the whole graph too.
            reach = {pin: len(nx.descendants(region, pin)) for pin in tied}
            most = max(reach.values())
            tied = [pin for pin in tied if reach[pin] == most]
        return min(tied)

    def lightest_path(
        self,
        source: str,
        sink: str,
        weight: Callable[[TimingEdge], Decimal | None],
    ) -> tuple[Decimal, list[TimingEdge]] | None:
        """Return the path whose arc weights sum least, with that sum.

        *weight* gives each arc its cost, or None to leave the arc out; of
        parallel arcs the lightest counts.  Unlike {func}`fastest_path`,
        which composes each transition along the path, this sums whatever
        number *weight* gives, such as each arc's own worst transition.

        Parameters
        ----------
        source : str
            The pin to start from.
        sink : str
            The pin to end at.
        weight : Callable[[TimingEdge], Decimal | None]
            The cost of an arc; never negative.

        Returns
        -------
        tuple[Decimal, list[TimingEdge]] | None
            The summed weight and the arcs of the path, ``(0, [])`` when
            *source* is *sink*, or None if no path joins them.

        Raises
        ------
        TimingGraphError
            If *weight* gives a negative cost to an arc the search meets.
        networkx.NodeNotFound
            If *source* or *sink* is not a pin of the graph.

        Examples
        --------
        >>> from sdf_toolkit.core.model import Corner
        >>> from sdf_toolkit.parser.parser import parse_sdf
        >>> sdf = parse_sdf(
        ...     '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "TOP") (INSTANCE)'
        ...     " (DELAY (ABSOLUTE (INTERCONNECT a b (1) (4))"
        ...     " (INTERCONNECT b c (4) (1)) (INTERCONNECT a c (3) (3))))))"
        ... )
        >>> total, arcs = TimingGraph(sdf).lightest_path(
        ...     "a", "c", lambda arc: arc.delay.scalar(corner=Corner.MAX)
        ... )
        >>> total, [arc.sink for arc in arcs]
        (Decimal('3'), ['c'])
        """
        _require_node(self._graph, source)
        _require_node(self._graph, sink)
        if source == sink:
            return Decimal(0), []
        lightest: dict[tuple[str, str], tuple[Decimal, TimingEdge] | None] = {}

        def cost(
            u: str, v: str, keyed: dict[int, dict[str, TimingEdge]]
        ) -> Decimal | None:
            if (u, v) not in lightest:
                best: tuple[Decimal, TimingEdge] | None = None
                for attrs in keyed.values():
                    arc = attrs[ARC_ATTR]
                    value = weight(arc)
                    if value is None:
                        continue
                    if value < 0:
                        msg = (
                            f"lightest_path() needs non-negative weights, but the "
                            f"arc {arc.source} -> {arc.sink} weighs {value}"
                        )
                        raise TimingGraphError(msg)
                    if best is None or value < best[0]:
                        best = (value, arc)
                lightest[u, v] = best
            found = lightest[u, v]
            return None if found is None else found[0]

        try:
            pins = nx.dijkstra_path(self._graph, source, sink, weight=cost)
        except nx.NetworkXNoPath:
            return None
        chosen = [lightest[u, v] for u, v in pairwise(pins)]
        arcs = [arc for _, arc in filter(None, chosen)]
        return sum((value for value, _ in filter(None, chosen)), Decimal(0)), arcs

    def register_inputs(self) -> list[RegisterInput]:
        """Return each register data pin with its clock pin and check limits.

        One item per data and clock pin pair of a SETUP, HOLD or SETUPHOLD
        check, in the order the first check of each pair appears, whether or
        not ``traverse_registers`` added arcs for them.  A graph from
        {meth}`within` keeps the pairs whose two pins are inside its instance.
        """
        return list(self._register_inputs)

    def _crosses_register(self, arc: TimingEdge) -> bool:
        """Report whether *arc* is a setup arc or an IOPATH of a register."""
        if arc.entry_type in (EntryType.SETUP, EntryType.SETUPHOLD):
            return True
        return (
            arc.entry_type is EntryType.IOPATH
            and arc.instance in self._register_instances
        )

    def _data(self) -> nx.MultiDiGraph:
        """Return the graph without the arcs that cross a register."""
        if self._data_graph is None:
            self._data_graph = self.combinational().graph
        return self._data_graph

    def _clocks(self) -> nx.MultiDiGraph:
        """Return the graph without the setup arcs of ``traverse_registers``."""
        if self._clock_graph is None:
            self._clock_graph = self._derived(
                arcs=lambda arc: (
                    arc.entry_type not in (EntryType.SETUP, EntryType.SETUPHOLD)
                )
            ).graph
        return self._clock_graph

    def combinational(self) -> "TimingGraph":
        """Return a copy without the arcs that cross a register.

        The copy drops the setup arcs ``traverse_registers`` adds and every
        IOPATH of a register, an instance with a SETUP, HOLD or SETUPHOLD
        check, so no path in it passes a clock edge.  Every pin stays, and
        pins and arcs keep the order of this graph.

        Examples
        --------
        >>> from sdf_toolkit.parser.parser import parse_sdf
        >>> sdf = parse_sdf(
        ...     '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "DFF") (INSTANCE ff)'
        ...     " (DELAY (ABSOLUTE (IOPATH CLK Q (1))))"
        ...     " (TIMINGCHECK (SETUP D (posedge CLK) (1)))))"
        ... )
        >>> [(a.source, a.sink) for a in TimingGraph(sdf).edges()]
        [('ff/CLK', 'ff/Q'), ('ff/D', 'ff/CLK')]
        >>> TimingGraph(sdf).combinational().edges()
        []
        """
        return self._derived(arcs=lambda arc: not self._crosses_register(arc))

    def register_input_timing(
        self,
        source: str,
        clock: str,
        register: RegisterInput,
        *,
        late: Corner = Corner.MAX,
        early: Corner = Corner.MIN,
    ) -> RegisterInputTiming | None:
        """Return the setup and hold requirement at *source* for *register*.

        Data runs from *source* to the register's data pin along paths that
        cross no register ({meth}`combinational`).  The clock runs from
        *clock* to the register's clock pin along any path but a setup arc,
        so it may pass clock buffers, clock gates and the clock-to-output
        arc of a clock divider.  A late arrival is the longest path at
        *late* over every transition, and an early arrival the shortest path
        at *early* over every transition.  Each limit is the largest at
        *late* over the register's checks, so of the edge and condition
        variants the worst counts.  Then

        - setup = late data + largest setup limit - early clock
        - hold = largest hold limit + late clock - early data

        A pin that is its own target arrives at zero.

        Parameters
        ----------
        source : str
            The pin the data starts at.
        clock : str
            The pin the clock starts at.
        register : RegisterInput
            The register input, from {meth}`register_inputs`.
        late : Corner, optional
            Corner of late arrivals and of the limits, by default MAX.
        early : Corner, optional
            Corner of early arrivals, by default MIN.

        Returns
        -------
        RegisterInputTiming | None
            The requirements, or None if *source* does not reach the data
            pin or *clock* does not reach the clock pin.

        Raises
        ------
        TimingLoopError
            If a data or clock path passes through a loop.
        networkx.NodeNotFound
            If *source*, *clock* or a register pin is not a pin of the graph.

        Examples
        --------
        >>> from sdf_toolkit.parser.parser import parse_sdf
        >>> sdf = parse_sdf(
        ...     '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "TOP") (INSTANCE)'
        ...     " (DELAY (ABSOLUTE (INTERCONNECT d ff/D (1:2:3))"
        ...     " (INTERCONNECT c ff/CLK (0.5:0.5:0.5)))))"
        ...     ' (CELL (CELLTYPE "DFF") (INSTANCE ff)'
        ...     " (TIMINGCHECK (SETUP D (posedge CLK) (0.1:0.1:0.2))"
        ...     " (HOLD D (posedge CLK) (0.05:0.05:0.1)))))"
        ... )
        >>> g = TimingGraph(sdf, traverse_registers=False)
        >>> (register,) = g.register_inputs()
        >>> g.register_input_timing("d", "c", register)
        RegisterInputTiming(setup=Decimal('2.7'), hold=Decimal('-0.4'))
        """
        for pin in (source, clock, register.data, register.clock):
            _require_node(self._graph, pin)
        data = _searches(self._data())
        clocks = _searches(self._clocks())
        if not data.reaches(source, register.data):
            return None
        if not clocks.reaches(clock, register.clock):
            return None
        late_data = data.arrival(source, register.data, corner=late, slowest=True)
        early_data = data.arrival(source, register.data, corner=early, slowest=False)
        late_clock = clocks.arrival(clock, register.clock, corner=late, slowest=True)
        early_clock = clocks.arrival(clock, register.clock, corner=early, slowest=False)
        setup = _largest(register.setup, late)
        hold = _largest(register.hold, late)
        return RegisterInputTiming(
            setup=None
            if setup is None or late_data is None or early_clock is None
            else late_data + setup - early_clock,
            hold=None
            if hold is None or late_clock is None or early_data is None
            else hold + late_clock - early_data,
        )

    def _derived(
        self,
        *,
        pins: Callable[[str], bool] = lambda _: True,
        arcs: Callable[[TimingEdge], bool] = lambda _: True,
        register_inputs: tuple[RegisterInput, ...] | None = None,
    ) -> "TimingGraph":
        """Return a copy with the *pins* and the *arcs* between them it keeps.

        Pins keep their order, and arcs enter in the order they entered this
        graph, so every pin lists its arcs out and in as it does here.  A
        subgraph of networkx would order pins by a Python set once it keeps
        under half of them, and its copy would order the arcs into a pin by
        their source pins.
        """
        graph = nx.MultiDiGraph()
        graph.add_nodes_from(pin for pin in self._graph if pins(pin))
        kept = tuple(
            arc
            for arc in self._arcs
            if pins(arc.source) and pins(arc.sink) and arcs(arc)
        )
        for arc in kept:
            graph.add_edge(arc.source, arc.sink, **{ARC_ATTR: arc})
        derived = copy.copy(self)
        # derived is a copy of self, so these are this class's own attributes.
        derived._graph = graph  # noqa: SLF001
        derived._arcs = kept  # noqa: SLF001
        derived._data_graph = None  # noqa: SLF001
        derived._clock_graph = None  # noqa: SLF001
        nx.freeze(graph)
        if register_inputs is not None:
            derived._register_inputs = register_inputs  # noqa: SLF001
        return derived

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
        {data}`~sdf_toolkit.core.model.DEFAULT_TOLERANCE`.

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


def _transitions(edge: EdgeSelect, *, slowest: bool = True) -> tuple[Transition, ...]:
    """Transitions whose extreme paths together give the *edge* extreme.

    A longest-path search takes the maximum over transitions, so it answers
    RISE, FALL and WORST; a shortest-path search takes the minimum and
    answers RISE, FALL and BEST.

    Raises
    ------
    TimingGraphError
        For BEST in a longest-path search or WORST in a shortest-path one.
    """
    match edge:
        case EdgeSelect.RISE:
            return (Transition.T01,)
        case EdgeSelect.FALL:
            return (Transition.T10,)
        case EdgeSelect.WORST if slowest:
            return tuple(Transition)
        case EdgeSelect.BEST if not slowest:
            return tuple(Transition)
        case EdgeSelect.BEST:
            msg = (
                "EdgeSelect.BEST takes the smallest transition of each path, and "
                "the path maximising that minimum has no longest-path form. Use "
                "rank_paths, which enumerates every path, to rank by BEST."
            )
            raise TimingGraphError(msg)
        case EdgeSelect.WORST:
            msg = (
                "EdgeSelect.WORST takes the largest transition of each path, and "
                "the path minimising that maximum has no shortest-path form. Use "
                "rank_paths with descending=False, which enumerates every path, to "
                "rank by WORST."
            )
            raise TimingGraphError(msg)


def _transition_delay(
    delay: DelayValues, transition: Transition, corner: Corner
) -> Decimal | None:
    """Value of *delay* for *transition* at *corner*, None where the file has none."""
    if transition in _X_TRANSITIONS and len(delay.rvalues) != 12:
        return None
    return delay.for_transition(transition).at(corner)


# Pin -> (latest or earliest arrival, arc it arrives through); the source has
# no arc.
_Arrivals = dict[str, tuple[Decimal, TimingEdge | None]]


def _arrivals(
    graph: nx.MultiDiGraph,
    *,
    source: str,
    order: list[str],
    transition: Transition,
    corner: Corner,
    slowest: bool = True,
) -> _Arrivals:
    """Relax arcs in topological *order* from *source* to get extreme arrivals.

    *slowest* keeps the latest arrival at each pin, otherwise the earliest;
    of equal arrivals the first relaxed stays.  An arc without a value for
    *transition* at *corner* carries no arrival, as composing it would leave
    that transition without a value.
    """
    better = operator.gt if slowest else operator.lt
    best: _Arrivals = {source: (Decimal(0), None)}
    for node in order:
        if node not in best:
            continue
        arrival = best[node][0]
        for _, succ, arc in graph.out_edges(node, data=ARC_ATTR):
            weight = _transition_delay(arc.delay, transition, corner)
            if weight is None:
                continue
            candidate = arrival + weight
            if succ not in best or better(candidate, best[succ][0]):
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


class _Searches:
    """What the searches learn about one frozen graph, kept for later searches.

    It holds the pins downstream of each source and upstream of each sink,
    the transitions worth a pass, and the extreme arrivals already found.
    Every value is what the search would compute afresh, so a kept value
    never changes a result.
    """

    def __init__(self, g: nx.MultiDiGraph) -> None:
        self.g = g
        self._downstream: dict[str, set[str]] = {}
        self._upstream: dict[str, set[str]] = {}
        self._arrivals: dict[tuple[str, str, Corner, bool], Decimal | None] = {}
        # Only a 12-rvalue delay times the X transitions; without one, a pass
        # for them reaches no pin past its start.
        self.timed_x = any(
            len(arc.delay.rvalues) == 12 for _, _, arc in g.edges(data=ARC_ATTR)
        )

    def downstream(self, source: str) -> set[str]:
        if source not in self._downstream:
            self._downstream[source] = nx.descendants(self.g, source)
        return self._downstream[source]

    def upstream(self, sink: str) -> set[str]:
        if sink not in self._upstream:
            self._upstream[sink] = nx.ancestors(self.g, sink)
        return self._upstream[sink]

    def reaches(self, source: str, sink: str) -> bool:
        return source == sink or sink in self.downstream(source)

    def cone_order(self, source: str, sink: str) -> list[str] | None:
        """Topological order of the pins on paths from *source* to *sink*.

        A pin is its own cone.  None if *sink* is not reached.  The order is
        the one ``networkx.topological_sort`` gives the cone: generation by
        generation from *source*, each pin's successors in arc order.

        Raises
        ------
        TimingLoopError
            If a path from *source* to *sink* passes through a loop.
        """
        if source == sink:
            return [source]
        if sink not in self.downstream(source):
            return None
        cone = (self.downstream(source) | {source}) & (self.upstream(sink) | {sink})
        pred, succ = self.g.pred, self.g.succ
        waiting = {
            pin: sum(len(keys) for u, keys in pred[pin].items() if u in cone)
            for pin in cone
        }
        order: list[str] = []
        generation = [source] if waiting[source] == 0 else []
        while generation:
            order.extend(generation)
            following: list[str] = []
            for pin in generation:
                for child, keys in succ[pin].items():
                    if child not in cone:
                        continue
                    waiting[child] -= len(keys)
                    if waiting[child] == 0:
                        following.append(child)
            generation = following
        if len(order) != len(cone):
            raise TimingLoopError(
                cycle=_cycle(self.g.subgraph(cone)), source=source, sink=sink
            )
        return order

    def arrival(
        self, source: str, sink: str, *, corner: Corner, slowest: bool
    ) -> Decimal | None:
        """Latest or earliest arrival at *sink* over every transition, or None."""
        key = (source, sink, corner, slowest)
        if key not in self._arrivals:
            edge = EdgeSelect.WORST if slowest else EdgeSelect.BEST
            found = _extreme_path(
                self.g, source, sink, corner=corner, edge=edge, slowest=slowest
            )
            self._arrivals[key] = None if found is None else found[0]
        return self._arrivals[key]


_SEARCHES: "weakref.WeakKeyDictionary[nx.MultiDiGraph, _Searches]" = (
    weakref.WeakKeyDictionary()
)


def _searches(g: nx.MultiDiGraph) -> _Searches:
    """Return what the searches know of *g*, kept while *g* lives.

    Only a frozen graph keeps it, since a change to the graph would make it
    stale; any other graph gets a fresh record each time.
    """
    if not nx.is_frozen(g):
        return _Searches(g)
    if g not in _SEARCHES:
        _SEARCHES[g] = _Searches(g)
    return _SEARCHES[g]


def _extreme_path(
    g: nx.MultiDiGraph,
    source: str,
    sink: str,
    *,
    corner: Corner,
    edge: EdgeSelect,
    slowest: bool,
    through: str | None = None,
) -> tuple[Decimal, list[TimingEdge]] | None:
    """Delay and arcs of the slowest or fastest path with a value.

    A path through *through* joins the extreme path of each transition from
    *source* to *through* to the one from *through* to *sink*.  A source
    that is its sink gives a zero delay and no arcs.  None if no path has a
    value.
    """
    transitions = _transitions(edge, slowest=slowest)
    stops = [source, sink] if through is None else [source, through, sink]
    for pin in stops:
        _require_node(g, pin)
    searches = _searches(g)
    if not searches.timed_x:
        # An X pass finds no path, and on a leg of one pin it only ties with
        # an earlier transition, which a tie never replaces.
        transitions = tuple(t for t in transitions if t not in _X_TRANSITIONS)
    legs: list[tuple[str, str, list[str]]] = []
    for start, end in pairwise(stops):
        order = searches.cone_order(start, end)
        if order is None:
            return None
        legs.append((start, end, order))
    better = operator.gt if slowest else operator.lt
    found: tuple[Decimal, list[TimingEdge]] | None = None
    for transition in transitions:
        total = Decimal(0)
        edges: list[TimingEdge] = []
        for start, end, order in legs:
            arrivals = _arrivals(
                g,
                source=start,
                order=order,
                transition=transition,
                corner=corner,
                slowest=slowest,
            )
            if end not in arrivals:
                break
            total += arrivals[end][0]
            edges += _backtrack(arrivals, end)
        else:
            if found is None or better(total, found[0]):
                found = (total, edges)
    return found


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
        {func}`~sdf_toolkit.core.model.to_decimal`.
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
