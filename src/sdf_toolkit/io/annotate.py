"""Verilog SDF back-annotation via specify blocks and net delays.

Yosys reads the Verilog and supplies every structural fact: modules, ports,
cells, nets and the byte span of each in the source.  Every text edit lands at
a position derived from those spans.  Yosys drops specify blocks and keyword
positions, so a comment-aware tokenizer runs inside a Yosys-reported module
span to find the ``endmodule`` keyword, an existing ``specify`` block and the
shape of a net declaration.  The tokenizer never decides which module, net or
cell an edit targets.

Any SDF content without a faithful Verilog form raises
{class}`AnnotationError` naming the entry, rather than being dropped.
"""

import bisect
import json
import string
import subprocess
import tempfile
from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, assert_never

from sdf_toolkit.core.model import (
    Arrival,
    BidirectSkew,
    Cond,
    CondElse,
    Corner,
    DelayMode,
    Departure,
    Device,
    DiffConstraint,
    Divider,
    EdgeSelect,
    EdgeType,
    Entry,
    Hold,
    Interconnect,
    Iopath,
    Label,
    MissingDelayError,
    NetDelay,
    NoChange,
    PathConstraint,
    PathPulse,
    PathPulsePercent,
    Period,
    PeriodConstraint,
    Port,
    PortSpec,
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
    TimingPortSpec,
    Values,
    Waveform,
    Width,
    format_whole,
    group_by_key,
)
from sdf_toolkit.errors import SDFToolkitError
from sdf_toolkit.parser.standard import SDFStandard


class AnnotationError(SDFToolkitError, ValueError):
    """The SDF or the Verilog holds something annotation cannot express."""


class YosysError(SDFToolkitError, RuntimeError):
    """Yosys is not on PATH, or it exits non-zero on the Verilog."""


class TimingCheckKind(StrEnum):
    """Verilog timing-check system task, named without its ``$``."""

    SETUP = "setup"
    HOLD = "hold"
    SETUPHOLD = "setuphold"
    WIDTH = "width"
    PERIOD = "period"
    RECOVERY = "recovery"
    REMOVAL = "removal"


class PortDirection(StrEnum):
    """Port direction in a Yosys module."""

    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"


# ── Yosys JSON schema (``write_json``) ──────────────────────────────


class YosysPortJson(TypedDict):
    """A module port; Yosys omits ``offset`` 0 and ``upto`` 0."""

    direction: Literal["input", "output", "inout"]
    bits: list[int | str]
    offset: NotRequired[int]
    upto: NotRequired[int]
    signed: NotRequired[int]


class YosysCellJson(TypedDict):
    """A cell instance."""

    hide_name: int
    type: str
    parameters: dict[str, str]
    attributes: dict[str, str]
    port_directions: NotRequired[dict[str, str]]
    connections: dict[str, list[int | str]]


class YosysNetJson(TypedDict):
    """A named net; Yosys omits ``offset`` 0 and ``upto`` 0."""

    hide_name: int
    bits: list[int | str]
    attributes: dict[str, str]
    offset: NotRequired[int]
    upto: NotRequired[int]
    signed: NotRequired[int]


class YosysModuleJson(TypedDict):
    """A module."""

    attributes: dict[str, str]
    ports: dict[str, YosysPortJson]
    cells: dict[str, YosysCellJson]
    netnames: dict[str, YosysNetJson]


class YosysJson(TypedDict):
    """The top level of ``write_json`` output."""

    creator: str
    modules: dict[str, YosysModuleJson]


# ── Yosys design model ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A Yosys ``src`` attribute: 1-based lines and byte columns, end exclusive."""

    path: Path
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    @classmethod
    def parse(cls, text: str) -> "SourceSpan":
        """Parse ``file:L.C-L.C``.

        Raises
        ------
        AnnotationError
            If *text* is not one span, as for an object Yosys merged from
            several places (``a|b``).
        """
        path, _, span = text.rpartition(":")
        start, _, end = span.partition("-")
        start_line, _, start_column = start.partition(".")
        end_line, _, end_column = end.partition(".")
        numbers = (start_line, start_column, end_line, end_column)
        if "|" in text or not path or not all(n.isdigit() for n in numbers):
            msg = f"Yosys src attribute {text!r} is not one file:L.C-L.C span"
            raise AnnotationError(msg)
        return cls(
            path=Path(path),
            start_line=int(start_line),
            start_column=int(start_column),
            end_line=int(end_line),
            end_column=int(end_column),
        )

    def __str__(self) -> str:
        """Return the span as ``file:line``."""
        return f"{self.path}:{self.start_line}"


def _optional_src(attributes: Mapping[str, str]) -> SourceSpan | None:
    """Return the ``src`` span, or None for an object Yosys made without one."""
    return SourceSpan.parse(attributes["src"]) if "src" in attributes else None


@dataclass
class YosysPort:
    """A port in a Yosys module.

    ``bits`` runs from the least significant bit.  ``offset`` is the lowest
    declared index and ``upto`` marks an ascending ``[lo:hi]`` range.
    """

    name: str
    direction: PortDirection
    bits: list[int | str] = field(default_factory=list)
    offset: int = 0
    upto: bool = False

    def bit_position(self, index: int) -> int:
        """Return the position in ``bits`` of the declared bit *index*.

        Raises
        ------
        AnnotationError
            If the port has no bit *index*.
        """
        relative = index - self.offset
        width = len(self.bits)
        if not 0 <= relative < width:
            msg = f"Port {self.name} has no bit {index}"
            raise AnnotationError(msg)
        return width - 1 - relative if self.upto else relative

    def bit_index(self, position: int) -> int:
        """Return the declared index of the bit at *position* in ``bits``.

        Raises
        ------
        AnnotationError
            If ``bits`` has no *position*.
        """
        width = len(self.bits)
        if not 0 <= position < width:
            msg = f"Port {self.name} has no bit position {position}"
            raise AnnotationError(msg)
        return self.offset + (width - 1 - position if self.upto else position)


@dataclass
class YosysCell:
    """A cell instance in a Yosys module."""

    name: str
    cell_type: str
    connections: dict[str, list[int | str]] = field(default_factory=dict)
    src: SourceSpan | None = None


@dataclass(frozen=True)
class YosysNet:
    """A named net; ``hidden`` marks a name Yosys generated."""

    name: str
    bits: list[int | str]
    hidden: bool
    src: SourceSpan | None


@dataclass
class YosysModule:
    """A module parsed from Yosys JSON output."""

    name: str
    ports: dict[str, YosysPort] = field(default_factory=dict)
    cells: dict[str, YosysCell] = field(default_factory=dict)
    netnames: dict[str, YosysNet] = field(default_factory=dict)
    src: SourceSpan | None = None


@dataclass
class YosysDesign:
    """Top-level Yosys design containing multiple modules."""

    modules: dict[str, YosysModule] = field(default_factory=dict)


# ── Yosys integration ───────────────────────────────────────────────


@contextmanager
def yosys_running(
    verilog_path: Path, *, library: Sequence[Path] = (), top: str | None = None
) -> Iterator[Callable[[], YosysJson]]:
    """Start Yosys on a Verilog file and yield a function that waits for its JSON.

    Each *library* file is read as blackbox cells, and *top* names the module
    the hierarchy is checked from.  A Yosys still running when the block exits
    is killed.

    Raises
    ------
    YosysError
        If Yosys is not on PATH.  The yielded function raises it when Yosys
        exits non-zero, and the message then carries its stderr.
    """
    # write_json rejects a module with an always block; proc -noopt lowers it
    # to hidden cells and leaves named cells and nets as written.
    views = "".join(f"read_verilog -lib {view.resolve()}; " for view in library)
    hierarchy = "" if top is None else f"hierarchy -top {top}; "
    script = (
        f"{views}read_verilog {verilog_path.resolve()}; {hierarchy}"
        "proc -noopt; write_json -"
    )
    cmd = ["yosys", "-q", "-p", script]
    # Files, because Yosys would stall on a full pipe until the caller reads it.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)  # noqa: S603
        except FileNotFoundError:
            msg = (
                "Yosys is required for Verilog annotation but was not found. "
                "Install it from https://github.com/YosysHQ/yosys"
            )
            raise YosysError(msg) from None

        def result() -> YosysJson:
            returncode = process.wait()
            if returncode != 0:
                stderr.seek(0)
                text = stderr.read().decode(errors="replace")
                msg = f"Yosys failed (exit {returncode}):\n{text}"
                raise YosysError(msg)
            stdout.seek(0)
            data: YosysJson = json.load(stdout)
            return data

        try:
            yield result
        finally:
            process.kill()
            process.wait()


def run_yosys(verilog_path: Path) -> YosysJson:
    """Parse a Verilog file with Yosys and return its ``write_json`` output.

    Raises
    ------
    YosysError
        If Yosys is not on PATH, or exits non-zero; the message then carries
        its stderr.
    """
    with yosys_running(verilog_path) as result:
        return result()


def parse_yosys_json(json_data: YosysJson) -> YosysDesign:
    """Build a YosysDesign from ``write_json`` output.

    Raises
    ------
    KeyError
        If a field ``write_json`` always writes is missing.
    """
    design = YosysDesign()
    for mod_name, mod_data in json_data["modules"].items():
        design.modules[mod_name] = YosysModule(
            name=mod_name,
            ports={
                name: YosysPort(
                    name=name,
                    direction=PortDirection(port["direction"]),
                    bits=port["bits"],
                    offset=port.get("offset", 0),
                    upto=port.get("upto", 0) == 1,
                )
                for name, port in mod_data["ports"].items()
            },
            cells={
                name: YosysCell(
                    name=name,
                    cell_type=cell["type"],
                    connections=cell["connections"],
                    src=_optional_src(cell["attributes"]),
                )
                for name, cell in mod_data["cells"].items()
            },
            netnames={
                name: YosysNet(
                    name=name,
                    bits=net["bits"],
                    hidden=net["hide_name"] == 1,
                    src=_optional_src(net["attributes"]),
                )
                for name, net in mod_data["netnames"].items()
            },
            src=_optional_src(mod_data["attributes"]),
        )
    return design


# ── SDF names ───────────────────────────────────────────────────────


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


# ── Specify block items ─────────────────────────────────────────────


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


@dataclass(frozen=True, slots=True)
class WireDelay:
    """A net delay for the declaration of ``net`` in ``module``.

    ``delays`` holds 1 to 3 ``min:typ:max`` values in Verilog net-delay
    order: rise, fall, turn-off.
    """

    module: str
    net: str
    delays: tuple[str, ...]


# ── SDF-to-module matching ──────────────────────────────────────────


def match_sdf_to_modules(sdf: SDFFile, design: YosysDesign) -> dict[str, list[Entry]]:
    """Collect the entries of every instance of each SDF cell type.

    Raises
    ------
    AnnotationError
        If a cell type has no module in the design.
    """
    missing = sorted(set(sdf.cells) - set(design.modules))
    if missing:
        msg = f"SDF cell types {missing} have no module in the Verilog"
        raise AnnotationError(msg)
    return {
        cell_type: [entry for entries in instances.values() for entry in entries]
        for cell_type, instances in sdf.cells.items()
    }


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


# ── Value formatting ────────────────────────────────────────────────


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


# ── SDF entries to specify items ────────────────────────────────────


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


# ── Specify block rendering ─────────────────────────────────────────


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


# ── INTERCONNECT to net delays ──────────────────────────────────────


def _find_cells(
    design: YosysDesign,
    *,
    module: YosysModule,
    path: tuple[str, ...],
    divider: Divider,
) -> list[tuple[YosysModule, YosysCell]]:
    """Return every (parent module, cell) that *path* names below *module*.

    A cell name may itself hold dividers, as in a flattened netlist, so
    each prefix of *path* is tried as one name.
    """
    found: list[tuple[YosysModule, YosysCell]] = []
    for split in range(1, len(path) + 1):
        name = divider.join(path[:split])
        if name not in module.cells:
            continue
        cell = module.cells[name]
        if split == len(path):
            found.append((module, cell))
        elif cell.cell_type in design.modules:
            child = design.modules[cell.cell_type]
            found.extend(
                _find_cells(design, module=child, path=path[split:], divider=divider)
            )
    return found


def _instance_counts(design: YosysDesign, top: str) -> Counter[str]:
    """Count the instances of each module in the hierarchy under *top*."""
    below: dict[str, Counter[str]] = {}

    def visit(name: str) -> Counter[str]:
        if name not in below:
            counts: Counter[str] = Counter()
            for cell in design.modules[name].cells.values():
                if cell.cell_type in design.modules:
                    counts[cell.cell_type] += 1
                    counts.update(visit(cell.cell_type))
            below[name] = counts
        return below[name]

    return visit(top) + Counter({top: 1})


def _named_nets_by_bit(module: YosysModule) -> dict[int, list[YosysNet]]:
    nets: dict[int, list[YosysNet]] = {}
    for net in module.netnames.values():
        if not net.hidden:
            for bit in net.bits:
                if isinstance(bit, int):
                    nets.setdefault(bit, []).append(net)
    return nets


def _load_net(
    pin: SdfPin,
    *,
    design: YosysDesign,
    top: YosysModule,
    divider: Divider,
    nets_by_bit: dict[str, dict[int, list[YosysNet]]],
) -> tuple[YosysModule, YosysNet]:
    """Return the module and the named net that drive the cell pin *pin*.

    *nets_by_bit* caches {func}`_named_nets_by_bit` per module name.
    """
    if not pin.instance:
        msg = f"load {pin.name} is a port of {top.name}; a port takes no net delay"
        raise AnnotationError(msg)
    cells = _find_cells(design, module=top, path=pin.instance, divider=divider)
    path = divider.join(pin.instance)
    if len(cells) != 1:
        msg = f"instance {path} matches {len(cells)} cells under {top.name}"
        raise AnnotationError(msg)
    module, cell = cells[0]
    if pin.name not in cell.connections:
        msg = f"instance {path} ({cell.cell_type}) has no pin {pin.name}"
        raise AnnotationError(msg)
    bits = cell.connections[pin.name]
    if pin.bit is None:
        if len(bits) != 1:
            msg = f"pin {pin.name} of {path} has {len(bits)} bits; select one"
            raise AnnotationError(msg)
        bit = bits[0]
    else:
        if cell.cell_type not in design.modules:
            msg = f"bit {pin.bit} of {path}/{pin.name} needs module {cell.cell_type}"
            raise AnnotationError(msg)
        port = design.modules[cell.cell_type].ports[pin.name]
        bit = bits[port.bit_position(pin.bit)]
    if isinstance(bit, str):
        msg = f"pin {pin.name} of {path} is tied to constant {bit!r}"
        raise AnnotationError(msg)

    if module.name not in nets_by_bit:
        nets_by_bit[module.name] = _named_nets_by_bit(module)
    index = nets_by_bit[module.name]
    if bit not in index:
        msg = f"pin {pin.name} of {path} is on no named net"
        raise AnnotationError(msg)
    nets = index[bit]
    if len(nets) != 1:
        msg = f"pin {pin.name} of {path} is on nets {[n.name for n in nets]}"
        raise AnnotationError(msg)
    net = nets[0]
    if len(net.bits) != 1:
        msg = f"net {net.name} is a vector; a net delay would apply to every bit"
        raise AnnotationError(msg)
    if net.name in module.ports:
        msg = f"net {net.name} is a port of {module.name}; a port takes no net delay"
        raise AnnotationError(msg)
    return module, net


def resolve_interconnects(sdf: SDFFile, design: YosysDesign) -> list[WireDelay]:
    """Resolve INTERCONNECT entries to net delays on the nets of their loads.

    A load path starts at the instance of the CELL that holds the entry,
    below the top module the SDF ``DESIGN`` names.  The delay lands on the
    one named net that drives the load pin, so every other load of that
    net sees it too.  The source port is not checked.

    Raises
    ------
    AnnotationError
        If the top module is unknown, a load does not resolve to one scalar
        non-port net in a module instantiated once, a delay is INCREMENT, has
        more than 3 rvalues or a value Verilog cannot express, or two loads
        put different delays on one net.
    """
    interconnects = [
        (instance, entry)
        for instances in sdf.cells.values()
        for instance, entries in instances.items()
        for entry in entries
        if isinstance(entry, Interconnect)
    ]
    if not interconnects:
        return []

    top_name = sdf.header.design
    if top_name is None or top_name not in design.modules:
        msg = f"INTERCONNECT needs the SDF DESIGN {top_name!r} as a Verilog module"
        raise AnnotationError(msg)
    top = design.modules[top_name]
    divider = sdf.header.effective_divider
    counts = _instance_counts(design, top_name)
    nets_by_bit: dict[str, dict[int, list[YosysNet]]] = {}

    resolved: dict[tuple[str, str], tuple[WireDelay, Interconnect]] = {}
    for instance, entry in interconnects:
        context = f"{top_name}: {entry.key}"
        if instance == "*":
            msg = f"{context}: INSTANCE * gives no path to a net"
            raise AnnotationError(msg)
        require_absolute(entry, context=context)
        rvalues = entry.delay.rvalues
        if len(rvalues) > 3:
            msg = (
                f"{context}: {len(rvalues)} rvalues; a Verilog net delay takes "
                "at most 3 (rise, fall, turn-off)"
            )
            raise AnnotationError(msg)
        load = f"{instance}{divider}{entry.load.port}" if instance else entry.load.port
        try:
            module, net = _load_net(
                SdfPin.parse(load, divider),
                design=design,
                top=top,
                divider=divider,
                nets_by_bit=nets_by_bit,
            )
        except AnnotationError as exc:
            raise AnnotationError(f"{context}: {exc}") from None
        if counts[module.name] != 1:
            msg = (
                f"{context}: net {net.name} is in {module.name}, instantiated "
                f"{counts[module.name]} times; its net delay would apply to all"
            )
            raise AnnotationError(msg)

        delay = WireDelay(
            module=module.name,
            net=net.name,
            delays=tuple(
                _mintypmax(v, context=f"{context} rvalue {i}")
                for i, v in enumerate(rvalues)
            ),
        )
        key = (module.name, net.name)
        if key in resolved and resolved[key][0] != delay:
            msg = (
                f"{context}: net {module.name}.{net.name} already takes "
                f"{resolved[key][0].delays} from {resolved[key][1].key}; one net "
                "holds one delay"
            )
            raise AnnotationError(msg)
        resolved[key] = (delay, entry)
    return [delay for delay, _ in resolved.values()]


# ── Source text and edits ───────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TextEdit:
    """Insert ``text`` before byte ``offset`` of the source."""

    offset: int
    text: str


@dataclass(frozen=True, slots=True)
class _Token:
    start: int
    end: int
    text: bytes


_WHITESPACE = frozenset(b" \t\r\n\f\v")
# Identifiers, keywords, numbers and based literals such as 4'b10x1.
_WORD = frozenset((string.ascii_letters + string.digits + "_$'").encode())
_NET_TYPES = frozenset(b"wire tri tri0 tri1 triand trior trireg wand wor uwire".split())
_NET_MODIFIERS = frozenset(b"signed vectored scalared".split())


@dataclass(frozen=True)
class VerilogSource:
    """The bytes of a Verilog file, addressed by Yosys ``src`` spans."""

    path: Path
    data: bytes
    line_starts: tuple[int, ...]
    # Module name to its tokens, filled on first use.
    _module_tokens: dict[str, list[_Token]] = field(
        default_factory=dict, compare=False, repr=False
    )

    @classmethod
    def read(cls, path: Path) -> "VerilogSource":
        """Read *path*; spans must name it by its resolved path."""
        data = path.resolve().read_bytes()
        starts = [0]
        newline = data.find(b"\n")
        while newline >= 0:
            starts.append(newline + 1)
            newline = data.find(b"\n", newline + 1)
        return cls(path=path.resolve(), data=data, line_starts=tuple(starts))

    def offsets(self, span: SourceSpan) -> tuple[int, int]:
        """Return the byte offsets of *span*, end exclusive.

        Raises
        ------
        AnnotationError
            If *span* lies in another file, as for an ``include``.
        """
        if span.path != self.path:
            msg = f"Yosys places {span} outside {self.path}; includes are not supported"
            raise AnnotationError(msg)
        start = self.line_starts[span.start_line - 1] + span.start_column - 1
        end = self.line_starts[span.end_line - 1] + span.end_column - 1
        return start, end

    def tokens(self, start: int, end: int) -> list[_Token]:
        """Split bytes *start* to *end* into tokens, dropping comments.

        A word (identifier, keyword or number), an escaped identifier and a
        string are one token each; any other byte is a token of its own.
        """
        data = self.data
        tokens: list[_Token] = []
        i = start
        while i < end:
            if data[i] in _WHITESPACE:
                i += 1
                continue
            if data.startswith(b"//", i):
                newline = data.find(b"\n", i)
                i = end if newline < 0 else newline
                continue
            if data.startswith(b"/*", i):
                close = data.find(b"*/", i + 2)
                if close < 0:
                    msg = f"{self.path}: unterminated block comment"
                    raise AnnotationError(msg)
                i = close + 2
                continue
            j = i + 1
            if data[i] == ord("\\"):
                while j < end and data[j] not in _WHITESPACE:
                    j += 1
            elif data[i] == ord('"'):
                while j < end and data[j] != ord('"'):
                    j += 2 if data[j] == ord("\\") else 1
                j += 1
            elif data[i] in _WORD:
                while j < end and data[j] in _WORD:
                    j += 1
            tokens.append(_Token(start=i, end=j, text=data[i:j]))
            i = j
        return tokens

    def module_tokens(self, module: YosysModule) -> list[_Token]:
        """Return the tokens of *module* from ``module`` to ``endmodule``."""
        if module.src is None:
            msg = f"Yosys gives no source span for module {module.name}"
            raise AnnotationError(msg)
        if module.name not in self._module_tokens:
            self._module_tokens[module.name] = self.tokens(*self.offsets(module.src))
        return self._module_tokens[module.name]

    def apply(self, edits: list[TextEdit]) -> str:
        """Return the source with every edit inserted."""
        pieces: list[bytes] = []
        position = 0
        for edit in sorted(edits, key=lambda e: e.offset):
            pieces += [self.data[position : edit.offset], edit.text.encode()]
            position = edit.offset
        pieces.append(self.data[position:])
        return b"".join(pieces).decode()


def specify_block_edit(
    source: VerilogSource, module: YosysModule, block: str
) -> TextEdit:
    """Place *block* on its own lines before the ``endmodule`` of *module*.

    Raises
    ------
    AnnotationError
        If the module already has a specify block.
    """
    tokens = source.module_tokens(module)
    if any(t.text == b"specify" for t in tokens):
        msg = f"module {module.name} already has a specify block"
        raise AnnotationError(msg)
    ends = [t for t in tokens if t.text == b"endmodule"]
    if not ends:
        msg = f"no endmodule inside the Yosys span of module {module.name}"
        raise AnnotationError(msg)
    offset = ends[-1].start
    line_start = source.data.rfind(b"\n", 0, offset) + 1
    if source.data[line_start:offset].strip():
        return TextEdit(offset=offset, text=f"\n{block}")
    return TextEdit(offset=line_start, text=block)


def wire_delay_edit(
    source: VerilogSource, module: YosysModule, delay: WireDelay
) -> TextEdit:
    """Insert ``#(delays)`` before the identifier in the declaration of a net.

    IEEE 1364-2005 puts a net delay after the net type and range, right
    before the identifiers, so the edit lands at the Yosys span of the net.

    Raises
    ------
    AnnotationError
        If the declaration is not ``<net type> [signed] [range] <net>;``.
        A declaration of several nets would give the delay to all of them.
    """
    net = module.netnames[delay.net]
    if net.src is None:
        msg = f"Yosys gives no source span for net {module.name}.{net.name}"
        raise AnnotationError(msg)
    start, end = source.offsets(net.src)
    tokens = source.module_tokens(module)
    index = bisect.bisect_left(tokens, start, key=lambda t: t.start)

    def fail(found: str) -> AnnotationError:
        return AnnotationError(
            f"net {module.name}.{net.name} at {net.src}: expected a declaration "
            f"'<net type> [range] {net.name};', found {found}"
        )

    if index == len(tokens) or (tokens[index].start, tokens[index].end) != (start, end):
        raise fail("no identifier at the Yosys span")
    after = tokens[index + 1].text if index + 1 < len(tokens) else b""
    j = index - 1
    if j >= 0 and tokens[j].text == b"]":
        depth = 0
        while j >= 0:
            if tokens[j].text == b"]":
                depth += 1
            elif tokens[j].text == b"[":
                depth -= 1
            j -= 1
            if depth == 0:
                break
    while j >= 0 and tokens[j].text in _NET_MODIFIERS:
        j -= 1
    before = tokens[j].text if j >= 0 else b""
    if before not in _NET_TYPES or after != b";":
        span = source.data[tokens[max(j, 0)].start : tokens[index].end]
        raise fail(repr(span.decode()) + f" followed by {after.decode()!r}")
    return TextEdit(offset=start, text=f"#({', '.join(delay.delays)}) ")


# ── Orchestrator ────────────────────────────────────────────────────


def annotate_verilog(
    sdf_path: Path,
    verilog_path: Path,
    *,
    output_path: Path | None = None,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
    standard: SDFStandard | None = None,
    loose_cond: bool = False,
) -> str:
    """Annotate a Verilog file with the timing of an SDF file.

    Each SDF cell type's module gets a specify block built from the
    worst-case entry per arc across its instances, ranked at *corner* with
    *edge*.  INTERCONNECT entries become net delays in the hierarchy under
    the SDF ``DESIGN`` module.  The annotated text is written to
    *output_path* when given, and returned.  *standard* and *loose_cond*
    set the rules the SDF is parsed under, as in
    {func}`sdf_toolkit.parser.parse_sdf`.

    Raises
    ------
    AnnotationError
        If any SDF content has no faithful Verilog form; see
        {func}`entries_to_specify` and {func}`resolve_interconnects`.
    YosysError
        If Yosys is missing or fails.
    """
    from sdf_toolkit.parser.parser import deferred_cyclic_gc, parse_sdf

    # The netlist and the SDF are independent until the modules are matched.
    with yosys_running(verilog_path) as yosys_json, deferred_cyclic_gc():
        sdf = parse_sdf(sdf_path.read_text(), standard=standard, loose_cond=loose_cond)
        design = parse_yosys_json(yosys_json())
        source = VerilogSource.read(verilog_path)
        divider = sdf.header.effective_divider

        edits: list[TextEdit] = []
        for mod_name, entries in match_sdf_to_modules(sdf, design).items():
            module = design.modules[mod_name]
            cell_entries = [e for e in entries if not isinstance(e, Interconnect)]
            worst = select_worst_case_delays(cell_entries, corner=corner, edge=edge)
            items = entries_to_specify(worst, module, divider=divider)
            if items:
                block = render_specify_block(items)
                edits.append(specify_block_edit(source, module, block))

        edits.extend(
            wire_delay_edit(source, design.modules[delay.module], delay)
            for delay in resolve_interconnects(sdf, design)
        )
        verilog_text = source.apply(edits)

    if output_path is not None:
        output_path.write_text(verilog_text)
    return verilog_text
