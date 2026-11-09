"""IEEE 1364-2005 specify items built from SDF delay and timing-check entries.

{func}`entries_to_specify` turns the entries of one cell type into
{class}`SpecifyPath` and {class}`SpecifyCheck` items, and
{func}`render_specify_block` writes them as Verilog text.  Any SDF content
without a faithful specify form raises
{class}`~sdf_toolkit.io.yosys.AnnotationError` naming the entry.
"""

import string
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import assert_never

from sdf_toolkit.core.checks import (
    BidirectSkew,
    Hold,
    NoChange,
    Period,
    Recovery,
    Recrem,
    Removal,
    Setup,
    SetupHold,
    Skew,
    Width,
)
from sdf_toolkit.core.delays import (
    Cond,
    CondElse,
    Device,
    Interconnect,
    Iopath,
    NetDelay,
    PathPulse,
    PathPulsePercent,
    Port,
    PortSpec,
    TimingPortSpec,
)
from sdf_toolkit.core.enums import Corner, DelayMode, Divider, EdgeSelect, EdgeType
from sdf_toolkit.core.sdffile import Entry, Label, group_by_key
from sdf_toolkit.core.timingenv import (
    Arrival,
    Departure,
    DiffConstraint,
    PathConstraint,
    PeriodConstraint,
    SkewConstraint,
    Slack,
    SumConstraint,
    Waveform,
)
from sdf_toolkit.core.values import MissingDelayError, Values, format_whole
from sdf_toolkit.io.yosys import AnnotationError, PortDirection, YosysModule


class TimingCheckKind(StrEnum):
    """Verilog timing-check system task, named without its ``$``."""

    SETUP = "setup"
    HOLD = "hold"
    SETUPHOLD = "setuphold"
    WIDTH = "width"
    PERIOD = "period"
    RECOVERY = "recovery"
    REMOVAL = "removal"


@dataclass(frozen=True, slots=True)
class SdfPin:
    r"""An SDF port path split at unescaped dividers, with escapes removed.

    ``bit`` is the index of an unescaped trailing ``[n]`` on the pin.  An
    escaped bracket stays part of the name, as in ``a\[0\]``.
    """

    instance: tuple[str, ...]
    name: str
    bit: int | None

    @classmethod
    def parse(cls, text: str, divider: Divider) -> "SdfPin":
        """Split *text* on *divider* and read an optional bit select.

        Raises
        ------
        AnnotationError
            If a path segment is empty or the bit select is not one index.
        """
        segments: list[str] = []
        current: list[str] = []
        bracket: int | None = None
        chars = iter(text)
        for char in chars:
            if char == "\\":
                escaped = next(chars, None)
                if escaped is None:
                    msg = f"SDF path {text!r} ends in a lone escape"
                    raise AnnotationError(msg)
                current.append(escaped)
                bracket = None
            elif char == divider:
                segments.append("".join(current))
                current = []
                bracket = None
            else:
                if char == "[":
                    bracket = len(current)
                current.append(char)
        segments.append("".join(current))
        if not all(segments):
            msg = f"SDF path {text!r} has an empty segment"
            raise AnnotationError(msg)

        name = segments[-1]
        bit = None
        if bracket is not None and name.endswith("]"):
            index = name[bracket + 1 : -1]
            if not index.isdigit():
                msg = f"SDF path {text!r} selects {index!r}; only one bit is supported"
                raise AnnotationError(msg)
            name, bit = name[:bracket], int(index)
        return cls(instance=tuple(segments[:-1]), name=name, bit=bit)


_IDENT_START = frozenset(string.ascii_letters + "_")


_IDENT_PART = _IDENT_START | frozenset(string.digits + "$")


def verilog_identifier(name: str) -> str:
    """Return *name* as a Verilog identifier, escaping it when it is not simple."""
    if name[0] in _IDENT_START and all(c in _IDENT_PART for c in name):
        return name
    return f"\\{name} "


@dataclass(frozen=True, slots=True)
class SpecifyEvent:
    """A timing-check event: ``[edge] terminal [&&& condition]``."""

    terminal: str
    edge: EdgeType | None = None
    condition: str | None = None


@dataclass(frozen=True, slots=True)
class SpecifyPath:
    """A module path delay; ``delays`` are ``min:typ:max`` in SDF rvalue order."""

    source: str
    destination: str
    delays: tuple[str, ...]
    source_edge: EdgeType | None = None
    condition: Cond | CondElse | None = None


@dataclass(frozen=True, slots=True)
class SpecifyCheck:
    """A timing check with its events and limits in Verilog argument order."""

    kind: TimingCheckKind
    events: tuple[SpecifyEvent, ...]
    limits: tuple[str, ...]


SpecifyItem = SpecifyPath | SpecifyCheck


def _entry_scalar(entry: Entry, corner: Corner, edge: EdgeSelect) -> Decimal | None:
    """Reduce *entry* to the number that ranks it, or None without a value.

    A delay ranks by its scalar, a check by its limit at *corner*, and a
    SETUPHOLD, RECREM or NOCHANGE by the width of its window, the sum of
    its two limits.
    """

    def window(first: Values, second: Values) -> Decimal | None:
        a, b = first.at(corner), second.at(corner)
        return None if a is None or b is None else a + b

    match entry:
        case Iopath() | Port() | Interconnect() | Device() | NetDelay() | Label():
            try:
                return entry.delay.scalar(corner=corner, edge=edge)
            except MissingDelayError:
                return None
        case PathPulse() | PathPulsePercent() | BidirectSkew():
            # No specify form, so entries_to_specify rejects it unranked.
            return None
        case Setup() | Hold() | Recovery() | Removal() | Skew() | Width() | Period():
            return entry.limit.at(corner)
        case SetupHold() | NoChange():
            return window(entry.setup, entry.hold)
        case Recrem():
            return window(entry.recovery, entry.removal)
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
            return None


def select_worst_case_delays(
    entries: list[Entry],
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> list[Entry]:
    """Keep the entry with the largest scalar at *corner* for each arc key.

    An entry without a value loses to one with a value; a tie keeps the
    first.  The result holds one entry per arc, in order of first appearance.
    """

    def rank(entry: Entry) -> tuple[bool, Decimal]:
        scalar = _entry_scalar(entry, corner, edge)
        return (False, Decimal(0)) if scalar is None else (True, scalar)

    return [max(group, key=rank) for group in group_by_key(entries).values()]


def _mintypmax(values: Values, *, context: str) -> str:
    """Format *values* as ``min:typ:max``.

    Raises
    ------
    AnnotationError
        If a corner is missing or not finite, since Verilog needs all three.
    """
    if values.is_empty():
        msg = f"{context}: an empty () value has no Verilog form"
        raise AnnotationError(msg)
    corners = (values.min, values.typ, values.max)
    present = [v for v in corners if v is not None]
    if len(present) != len(corners):
        written = ":".join("" if v is None else format_whole(v) for v in corners)
        msg = (
            f"{context}: ({written}) lacks a corner; "
            "a Verilog min:typ:max needs all three"
        )
        raise AnnotationError(msg)
    if not all(v.is_finite() for v in present):
        msg = f"{context}: {values} is not finite"
        raise AnnotationError(msg)
    return ":".join(format_whole(v) for v in present)


@dataclass(frozen=True, slots=True)
class _Terminal:
    text: str
    width: int
    direction: PortDirection


def _terminal(
    port: PortSpec | TimingPortSpec,
    *,
    module: YosysModule,
    divider: Divider,
    context: str,
) -> _Terminal:
    """Resolve an SDF cell port to a terminal of *module*."""
    pin = SdfPin.parse(port.port, divider)
    if pin.instance:
        msg = f"{context}: port {port.port} is hierarchical in a cell entry"
        raise AnnotationError(msg)
    if pin.name not in module.ports:
        msg = f"{context}: module {module.name} has no port {pin.name}"
        raise AnnotationError(msg)
    yosys_port = module.ports[pin.name]
    text = verilog_identifier(pin.name)
    if pin.bit is None:
        return _Terminal(
            text=text, width=len(yosys_port.bits), direction=yosys_port.direction
        )
    try:
        yosys_port.bit_position(pin.bit)
    except AnnotationError as exc:
        raise AnnotationError(f"{context}: {exc}") from None
    return _Terminal(text=f"{text}[{pin.bit}]", width=1, direction=yosys_port.direction)


def require_absolute(entry: Iopath | Interconnect | Port, *, context: str) -> None:
    """Raise for an INCREMENT, since Verilog delays have no increment form."""
    if entry.mode is DelayMode.INCREMENT:
        msg = (
            f"{context}: INCREMENT delays have no Verilog form, which states "
            "absolute delays only; add the increment to its ABSOLUTE base first"
        )
        raise AnnotationError(msg)


def _verilog_edge(edge: EdgeType | None, *, context: str) -> EdgeType | None:
    """Return *edge* if IEEE 1364 writes it as ``posedge`` or ``negedge``.

    Raises
    ------
    AnnotationError
        If *edge* names one transition, such as ``01``, which this writer
        does not map to an IEEE 1364 ``edge [...]`` control.
    """
    match edge:
        case None | EdgeType.POSEDGE | EdgeType.NEGEDGE:
            return edge
        case (
            EdgeType.E01
            | EdgeType.E10
            | EdgeType.E0Z
            | EdgeType.EZ1
            | EdgeType.E1Z
            | EdgeType.EZ0
        ):
            msg = f"{context}: edge {edge} has no posedge or negedge form"
            raise AnnotationError(msg)


def _path(
    entry: Iopath, *, module: YosysModule, divider: Divider, context: str
) -> SpecifyPath:
    require_absolute(entry, context=context)
    if entry.delay.limits is not None:
        msg = f"{context}: pulse limits in a delval have no specify path form"
        raise AnnotationError(msg)
    if entry.retain:
        msg = f"{context}: RETAIN has no IEEE 1364 specify path form"
        raise AnnotationError(msg)
    source = _terminal(entry.input, module=module, divider=divider, context=context)
    destination = _terminal(
        entry.output, module=module, divider=divider, context=context
    )
    if source.direction == PortDirection.OUTPUT:
        msg = f"{context}: path source {entry.input.port} is an output"
        raise AnnotationError(msg)
    if destination.direction == PortDirection.INPUT:
        msg = f"{context}: path destination {entry.output.port} is an input"
        raise AnnotationError(msg)
    if entry.output.edge is not None:
        msg = f"{context}: a Verilog path destination takes no edge"
        raise AnnotationError(msg)
    if source.width != destination.width:
        msg = (
            f"{context}: a parallel path joins {source.width} bits to "
            f"{destination.width}; widths must match"
        )
        raise AnnotationError(msg)
    if entry.input.edge is not None and isinstance(entry.cond, CondElse):
        msg = f"{context}: IEEE 1364 allows ifnone only on a path without an edge"
        raise AnnotationError(msg)
    return SpecifyPath(
        source=source.text,
        destination=destination.text,
        delays=tuple(
            _mintypmax(v, context=f"{context} rvalue {i}")
            for i, v in enumerate(entry.delay.rvalues)
        ),
        source_edge=_verilog_edge(entry.input.edge, context=context),
        condition=entry.cond,
    )


def _specify_item(
    entry: Entry, module: YosysModule, *, divider: Divider
) -> SpecifyItem:
    """Convert one entry of the cell *module* to a specify item."""
    context = f"{module.name}: {entry.key}"

    def check(
        kind: TimingCheckKind,
        ports: tuple[TimingPortSpec, ...],
        limits: tuple[Values, ...],
    ) -> SpecifyCheck:
        events = []
        for port in ports:
            terminal = _terminal(port, module=module, divider=divider, context=context)
            condition = None if port.cond is None else port.cond.equation
            edge = _verilog_edge(port.edge, context=context)
            events.append(SpecifyEvent(terminal.text, edge, condition))
        return SpecifyCheck(
            kind=kind,
            events=tuple(events),
            limits=tuple(f"({_mintypmax(v, context=context)})" for v in limits),
        )

    def edge_required(port: TimingPortSpec) -> tuple[TimingPortSpec]:
        if port.edge is None:
            msg = f"{context}: IEEE 1364 ${entry.type} needs an edge on its event"
            raise AnnotationError(msg)
        return (port,)

    match entry:
        case Iopath():
            return _path(entry, module=module, divider=divider, context=context)
        case Setup():
            ports = (entry.data, entry.reference)
            return check(TimingCheckKind.SETUP, ports, (entry.limit,))
        case Hold():
            ports = (entry.reference, entry.data)
            return check(TimingCheckKind.HOLD, ports, (entry.limit,))
        case SetupHold(scond=None, ccond=None):
            ports = (entry.reference, entry.data)
            limits = (entry.setup, entry.hold)
            return check(TimingCheckKind.SETUPHOLD, ports, limits)
        case SetupHold():
            msg = (
                f"{context}: SCOND and CCOND would be the $setuphold timestamp "
                "and timecheck conditions, which this writer does not emit"
            )
            raise AnnotationError(msg)
        case Recrem() | Skew() | NoChange():
            msg = (
                f"{context}: this writer does not emit ${entry.type}, since the "
                "SDF to IEEE 1364 port order of the check is not confirmed"
            )
            raise AnnotationError(msg)
        case BidirectSkew():
            msg = f"{context}: BIDIRECTSKEW has no IEEE 1364 system task"
            raise AnnotationError(msg)
        case Recovery():
            # SDF port order, as earlier releases wrote it.
            ports = (entry.data, entry.reference)
            return check(TimingCheckKind.RECOVERY, ports, (entry.limit,))
        case Removal():
            ports = (entry.data, entry.reference)
            return check(TimingCheckKind.REMOVAL, ports, (entry.limit,))
        case Width():
            ports = edge_required(entry.port)
            return check(TimingCheckKind.WIDTH, ports, (entry.limit,))
        case Period():
            ports = edge_required(entry.port)
            return check(TimingCheckKind.PERIOD, ports, (entry.limit,))
        case Port():
            msg = (
                f"{context}: a PORT delay has no specify form; rewrite it "
                "with port-to-interconnect first"
            )
            raise AnnotationError(msg)
        case Device():
            msg = f"{context}: DEVICE delays are not supported"
            raise AnnotationError(msg)
        case Interconnect():
            msg = f"{context}: an INTERCONNECT is a net delay, not a specify item"
            raise AnnotationError(msg)
        case NetDelay():
            msg = f"{context}: a NETDELAY is a net delay, not a specify item"
            raise AnnotationError(msg)
        case Label():
            msg = (
                f"{context}: a LABEL would be a specparam, which this writer "
                "does not emit"
            )
            raise AnnotationError(msg)
        case PathPulse() | PathPulsePercent():
            msg = (
                f"{context}: {entry.type.upper()} would be a PATHPULSE$ specparam, "
                "which this writer does not emit"
            )
            raise AnnotationError(msg)
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
            msg = f"{context}: {entry.type.upper()} has no Verilog form"
            raise AnnotationError(msg)
        case _:
            assert_never(entry)


def entries_to_specify(
    entries: list[Entry], module: YosysModule, *, divider: Divider
) -> list[SpecifyItem]:
    """Convert the IOPATH and timing-check entries of a cell to specify items.

    Two-port checks take their IEEE 1364-2005 argument order:
    ``$setup(data, reference)``, ``$hold(reference, data)`` and
    ``$setuphold(reference, data)``.  ``$recovery`` and ``$removal`` keep
    SDF port order.  Every limit is parenthesised, since a bare
    ``min:typ:max`` is not an IEEE 1364 expression.

    Raises
    ------
    AnnotationError
        If an entry names a port the module lacks, holds a value Verilog
        cannot express, or has no specify form (PORT, DEVICE, INTERCONNECT,
        PATHCONSTRAINT, an INCREMENT IOPATH, or a WIDTH or PERIOD without an
        edge).
    """
    return [_specify_item(entry, module, divider=divider) for entry in entries]


def _format_event(event: SpecifyEvent) -> str:
    text = event.terminal if event.edge is None else f"{event.edge} {event.terminal}"
    return text if event.condition is None else f"{text} &&& {event.condition}"


def format_specify_item(item: SpecifyItem) -> str:
    """Format one specify item as a Verilog statement.

    An edge-sensitive path takes the IEEE 1364-2005 form
    ``(posedge CLK => (Q : 1'bx))``.  SDF names no data source, and the
    standard states the data source expression does not affect propagation.
    """
    match item:
        case SpecifyPath():
            if item.source_edge is None:
                path = f"({item.source} => {item.destination})"
            else:
                path = (
                    f"({item.source_edge} {item.source} => ({item.destination} : 1'bx))"
                )
            statement = f"{path} = ({', '.join(item.delays)});"
            match item.condition:
                case Cond(equation=equation):
                    return f"if ({equation}) {statement}"
                case CondElse():
                    return f"ifnone {statement}"
                case None:
                    return statement
        case SpecifyCheck():
            args = [_format_event(e) for e in item.events] + list(item.limits)
            return f"${item.kind}({', '.join(args)});"
        case _:
            assert_never(item)


def render_specify_block(items: list[SpecifyItem]) -> str:
    """Render *items* as a ``specify ... endspecify`` block ending in a newline."""
    lines = "".join(f"        {format_specify_item(item)}\n" for item in items)
    return f"    specify\n{lines}    endspecify\n"
