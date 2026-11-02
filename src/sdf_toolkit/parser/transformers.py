"""SDF parse tree transformer that lowers Lark reductions into the data model.

The parser runs this transformer inline, so each rule method fires as Lark
reduces and no tree is built.  A delay entry reduces before the
``ABSOLUTE``/``INCREMENT`` block and any ``COND`` around it, yet the model
entries are frozen and need both, so the delay rules return a
{class}`_PendingDelay` that the enclosing block finalises.
"""

from collections.abc import Callable, Iterable
from typing import NamedTuple

from lark import Token, Transformer, v_args

from sdf_toolkit.core.model import (
    CellsDict,
    Cond,
    DelayEntry,
    DelayMode,
    DelayValues,
    Device,
    Divider,
    EdgeType,
    Entry,
    Hold,
    Interconnect,
    Iopath,
    PathConstraint,
    Period,
    Port,
    PortSpec,
    Recovery,
    Removal,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    Timescale,
    TimeUnit,
    TimingCheck,
    TimingPortSpec,
    Values,
    Width,
)


class CellBlock(NamedTuple):
    """One ``(CELL ...)`` block with its entries in file order."""

    celltype: str
    instance: str
    entries: list[Entry]


class ParsedChunk(NamedTuple):
    """Header fields and cell blocks of one parsed piece of an SDF file."""

    header: SDFHeader
    blocks: list[CellBlock]


class _PendingDelay(NamedTuple):
    """A delay entry waiting for the mode of its block and any ``COND``."""

    build: Callable[[DelayMode, Cond | None], DelayEntry]
    cond: Cond | None


_DelayItem = _PendingDelay | tuple["_DelayItem", ...] | None


def assemble_cells(blocks: Iterable[CellBlock]) -> CellsDict:
    """Build the nested cells mapping from cell blocks in file order.

    Blocks naming the same cell type and instance add to one entry list, in
    the order they appear in the file.
    """
    cells: CellsDict = {}
    for block in blocks:
        cells.setdefault(block.celltype, {}).setdefault(block.instance, []).extend(
            block.entries
        )
    return cells


def _flatten(items: Iterable[_DelayItem]) -> list[_PendingDelay]:
    """Flatten nested ``COND`` groups into one list, in file order."""
    flat: list[_PendingDelay] = []
    for item in items:
        if item is None:
            continue
        if isinstance(item, _PendingDelay):
            flat.append(item)
        else:
            flat.extend(_flatten(item))
    return flat


def remove_quotation(s: str) -> str:
    """Remove quotation marks from string."""
    return s.replace('"', "")


def _format_value(val: float | None) -> str:
    """Format a single timing value, using integer form when possible."""
    if val is None:
        return ""
    if val == int(val):
        return str(int(val))
    return str(val)


def _format_values_triple(v: Values) -> str:
    """Format a Values object as an SDF triple string (e.g. '5.5:5.0:4.5')."""
    return ":".join(_format_value(val) for val in (v.min, v.typ, v.max))


class SDFBlockTransformer(Transformer):
    """Transformer that collects the cell blocks of an SDF file unassembled.

    A chunk of a larger file returns its blocks as they are, and
    {func}`assemble_cells` joins the blocks of every chunk in file order.
    """

    def __init__(self) -> None:
        super().__init__()
        self.blocks: list[CellBlock] = []
        self.delays_list: list[Entry] = []

    def reset(self) -> None:
        """Drop the state of a previous parse so the instance can be reused."""
        self.blocks = []
        self.delays_list = []

    # ── Top-level structure ──────────────────────────────────────────

    def _parsed_chunk(self, items: tuple[SDFHeader | None, ...]) -> ParsedChunk:
        """Fold the header items of *items* into one header beside the blocks."""
        header = SDFHeader()
        for item in items:
            if item is not None:
                header = header.merged(item)
        return ParsedChunk(header=header, blocks=self.blocks)

    @v_args(inline=True)
    def sdf_file(self, _tag: Token, *items: SDFHeader | None) -> ParsedChunk:
        """Process the top-level SDF file structure."""
        return self._parsed_chunk(items)

    @v_args(inline=True)
    def head(self, _tag: Token, *items: SDFHeader | None) -> ParsedChunk:
        """Process a file cut short before its first cell."""
        return self._parsed_chunk(items)

    @v_args(inline=True)
    def body(self, *items: SDFHeader | None) -> ParsedChunk:
        """Process a run of items cut out of a file."""
        return self._parsed_chunk(items)

    def tail(self, _children: list[object]) -> ParsedChunk:
        """Process the parenthesis closing a file, which holds no items."""
        return ParsedChunk(header=SDFHeader(), blocks=self.blocks)

    @v_args(inline=True)
    def sdf_item(self, item: SDFHeader | None) -> SDFHeader | None:
        """Pass through a header item, or None for a cell."""
        return item

    @v_args(inline=True)
    def sdf_header_item(self, item: SDFHeader) -> SDFHeader:
        """Pass through header items."""
        return item

    # ── Header fields ────────────────────────────────────────────────

    @v_args(inline=True)
    def sdf_version(self, value: Token | None = None) -> SDFHeader:
        """Process SDF version header field."""
        return SDFHeader(sdfversion=remove_quotation(str(value)) if value else "")

    @v_args(inline=True)
    def design(self, value: Token | None = None) -> SDFHeader:
        """Process design name header field."""
        return SDFHeader(design=remove_quotation(str(value)) if value else "")

    @v_args(inline=True)
    def date(self, value: Token | None = None) -> SDFHeader:
        """Process date header field."""
        return SDFHeader(date=remove_quotation(str(value)) if value else "")

    @v_args(inline=True)
    def vendor(self, value: Token | None = None) -> SDFHeader:
        """Process vendor header field."""
        return SDFHeader(vendor=remove_quotation(str(value)) if value else "")

    @v_args(inline=True)
    def program(self, value: Token | None = None) -> SDFHeader:
        """Process program header field."""
        return SDFHeader(program=remove_quotation(str(value)) if value else "")

    @v_args(inline=True)
    def version(self, value: Token | None = None) -> SDFHeader:
        """Process version header field."""
        return SDFHeader(version=remove_quotation(str(value)) if value else "")

    @v_args(inline=True)
    def process(self, value: Token | None = None) -> SDFHeader:
        """Process process header field."""
        return SDFHeader(process=remove_quotation(str(value)) if value else "")

    @v_args(inline=True)
    def voltage(self, val: Values) -> SDFHeader:
        """Process voltage specification."""
        return SDFHeader(voltage=_format_values_triple(val))

    @v_args(inline=True)
    def temperature(self, val: Values) -> SDFHeader:
        """Process temperature specification."""
        return SDFHeader(temperature=_format_values_triple(val))

    @v_args(inline=True)
    def hierarchy_divider(self, val: Token) -> SDFHeader:
        """Process hierarchy divider."""
        return SDFHeader(divider=Divider(str(val)))

    @v_args(inline=True)
    def timescale(self, val: float, unit: Token) -> SDFHeader:
        """Process timescale specification.

        Raises
        ------
        ValueError
            If the unit or the number is not one IEEE 1497 allows.
        """
        return SDFHeader(timescale=Timescale(value=val, unit=TimeUnit(str(unit))))

    # ── Value processing ─────────────────────────────────────────────

    @v_args(inline=True)
    def rvalue(self, value: float | Values | None = None) -> Values:
        """Process an rvalue: a bare number, ``(number)``, a triple or ``()``."""
        if value is None:
            return Values()
        if isinstance(value, Values):
            return value
        return Values.uniform(value)

    @v_args(inline=True)
    def real_triple(
        self, min_val: float | None, typ_val: float | None, max_val: float | None
    ) -> Values:
        """Process real triple (min:typ:max)."""
        return Values(min=min_val, typ=typ_val, max=max_val)

    @v_args(inline=True)
    def delval_list(self, *rvalues: Values) -> DelayValues:
        """Process a delay value list; its positions are transitions.

        Raises
        ------
        ValueError
            If the list does not hold 1, 2, 3, 6 or 12 rvalues.
        """
        return DelayValues(rvalues=rvalues)

    # ── Cell structure ───────────────────────────────────────────────

    @v_args(inline=True)
    def cell(
        self,
        celltype: str,
        instance: str | None,
        _timing: object = None,
    ) -> None:
        """Close a cell block, taking the entries collected since the last."""
        self.blocks.append(
            CellBlock(
                celltype=str(celltype),
                instance="" if instance is None else str(instance),
                entries=self.delays_list,
            )
        )
        self.delays_list = []

    @v_args(inline=True)
    def celltype(self, val: Token) -> str:
        """Process cell type."""
        return remove_quotation(str(val))

    @v_args(inline=True)
    def instance(self, val: Token | None = None) -> str | None:
        """Process instance name."""
        if val is None:
            return None
        return str(val)

    # ── Delay blocks ─────────────────────────────────────────────────

    @v_args(inline=True)
    def delay_entry(self, item: _DelayItem) -> _DelayItem:
        """Unwrap delay entry."""
        return item

    def _finalise(self, items: Iterable[_DelayItem], mode: DelayMode) -> None:
        """Build the pending entries of a delay block with its *mode*."""
        self.delays_list.extend(
            pending.build(mode, pending.cond) for pending in _flatten(items)
        )

    @v_args(inline=True)
    def absolute(self, *items: _DelayItem) -> None:
        """Process absolute delay block."""
        self._finalise(items, DelayMode.ABSOLUTE)

    @v_args(inline=True)
    def increment(self, *items: _DelayItem) -> None:
        """Process increment delay block."""
        self._finalise(items, DelayMode.INCREMENT)

    # ── Delay types ──────────────────────────────────────────────────

    @v_args(inline=True)
    def iopath(
        self, input_port: PortSpec, output_port: PortSpec, delay: DelayValues
    ) -> _PendingDelay:
        """Process IOPATH delay specification."""
        return _PendingDelay(
            build=lambda mode, cond: Iopath(
                input=input_port, output=output_port, delay=delay, mode=mode, cond=cond
            ),
            cond=None,
        )

    @v_args(inline=True)
    def interconnect(
        self, source: PortSpec, load: PortSpec, delay: DelayValues
    ) -> _PendingDelay:
        """Process INTERCONNECT delay specification."""
        return _PendingDelay(
            build=lambda mode, cond: Interconnect(
                source=source, load=load, delay=delay, mode=mode, cond=cond
            ),
            cond=None,
        )

    @v_args(inline=True)
    def port(self, port_spec: PortSpec, delay: DelayValues) -> _PendingDelay:
        """Process PORT delay specification."""
        return _PendingDelay(
            build=lambda mode, cond: Port(
                port=port_spec, delay=delay, mode=mode, cond=cond
            ),
            cond=None,
        )

    @v_args(inline=True)
    def device(self, port_spec: PortSpec, delay: DelayValues) -> _PendingDelay:
        """Process DEVICE delay specification."""
        return _PendingDelay(
            build=lambda mode, cond: Device(
                output=port_spec, delay=delay, mode=mode, cond=cond
            ),
            cond=None,
        )

    # ── Port specification ───────────────────────────────────────────

    @v_args(inline=True)
    def port_spec(self, *args: Token) -> PortSpec:
        """Process port specification."""
        if len(args) == 1:
            return PortSpec(port=str(args[0]))
        if len(args) == 2:
            return PortSpec(port=str(args[1]), edge=EdgeType(str(args[0]).lower()))
        raise ValueError(f"Invalid port_spec args: {args}")

    @v_args(inline=True)
    def port_condition(self, token: Token) -> str:
        """Process port condition (posedge/negedge)."""
        return str(token)

    @v_args(inline=True)
    def timing_port(self, *args: PortSpec | str) -> TimingPortSpec:
        """Process a timing check port and the ``COND`` it may carry.

        Grammar: ``timing_port: port_spec | "(" "COND" equation port_spec ")"``
        """
        match args:
            case (PortSpec(port=port, edge=edge),):
                return TimingPortSpec(port=port, edge=edge)
            case (str(equation), PortSpec(port=port, edge=edge)):
                return TimingPortSpec(
                    port=port, edge=edge, cond=Cond(equation=equation)
                )
        raise ValueError(f"Invalid timing_port args: {args}")

    # ── Timing checks ────────────────────────────────────────────────

    @v_args(inline=True)
    def setup_check(
        self, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> Setup:
        """Process setup timing check."""
        return Setup(data=data, reference=reference, limit=limit)

    @v_args(inline=True)
    def hold_check(
        self, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> Hold:
        """Process hold timing check."""
        return Hold(data=data, reference=reference, limit=limit)

    @v_args(inline=True)
    def removal_check(
        self, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> Removal:
        """Process removal timing check."""
        return Removal(data=data, reference=reference, limit=limit)

    @v_args(inline=True)
    def recovery_check(
        self, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> Recovery:
        """Process recovery timing check."""
        return Recovery(data=data, reference=reference, limit=limit)

    @v_args(inline=True)
    def width_check(self, port: TimingPortSpec, limit: Values) -> Width:
        """Process width timing check."""
        return Width(port=port, limit=limit)

    @v_args(inline=True)
    def period_check(self, port: TimingPortSpec, limit: Values) -> Period:
        """Process period timing check."""
        return Period(port=port, limit=limit)

    @v_args(inline=True)
    def setuphold_check(
        self,
        data: TimingPortSpec,
        reference: TimingPortSpec,
        setup: Values,
        hold: Values,
    ) -> SetupHold:
        """Process setuphold timing check."""
        return SetupHold(data=data, reference=reference, setup=setup, hold=hold)

    @v_args(inline=True)
    def t_check(self, item: TimingCheck) -> TimingCheck:
        """Pass through timing check entry."""
        return item

    @v_args(inline=True)
    def timing_check_list(self, *items: TimingCheck) -> None:
        """Process timing check list."""
        self.delays_list.extend(items)

    # ── Conditional delays ───────────────────────────────────────────

    @v_args(inline=True)
    def cond_delay(self, condition: str, *items: _DelayItem) -> tuple[_DelayItem, ...]:
        """Attach a ``COND`` to every delay entry it wraps.

        Raises
        ------
        ValueError
            If a wrapped entry already carries a ``COND``.
        """
        cond = Cond(equation=condition)
        conditioned: list[_DelayItem] = []
        for pending in _flatten(items):
            if pending.cond is not None:
                msg = f"Nested COND ({condition}) around COND ({pending.cond.equation})"
                raise ValueError(msg)
            conditioned.append(pending._replace(cond=cond))
        return tuple(conditioned)

    @v_args(inline=True)
    def delay_condition(self, *args: str | Token) -> str:
        """Handle delay condition."""
        for arg in args:
            if isinstance(arg, str) and arg not in ("(", ")"):
                return arg
        return "".join(str(a) for a in args if str(a) not in ("(", ")"))

    @v_args(inline=True)
    def equation(self, *items: str | Token) -> str:
        """Process equation for conditions."""
        return " ".join(str(item) for item in items)

    @v_args(inline=True)
    def equation_item(self, item: Token) -> str:
        """Process equation items."""
        return str(item)

    # ── Constraints ──────────────────────────────────────────────────

    @v_args(inline=True)
    def path_constraint(
        self, first: PortSpec, last: PortSpec, rise: Values, fall: Values
    ) -> PathConstraint:
        """Process path constraint.

        Raises
        ------
        ValueError
            If a port carries an edge, which ``PATHCONSTRAINT`` does not allow.
        """
        for spec in (first, last):
            if spec.edge is not None:
                msg = f"PATHCONSTRAINT port ({spec.edge} {spec.port}) takes no edge"
                raise ValueError(msg)
        return PathConstraint(ports=(first.port, last.port), rise=rise, fall=fall)

    @v_args(inline=True)
    def constraints_list(self, *items: PathConstraint) -> None:
        """Process constraints list."""
        self.delays_list.extend(items)

    # ── Terminal values ──────────────────────────────────────────────

    @v_args(inline=True)
    def STRING(self, value: Token) -> str:  # noqa: N802
        """Convert STRING token to str."""
        return str(value)

    @v_args(inline=True)
    def FLOAT(self, value: Token) -> float:  # noqa: N802
        """Convert FLOAT token to float."""
        return float(value)

    @v_args(inline=True)
    def QSTRING(self, value: Token) -> str:  # noqa: N802
        """Convert QSTRING token to str."""
        return str(value)

    @v_args(inline=True)
    def QFLOAT(self, value: Token) -> str:  # noqa: N802
        """Convert QFLOAT token to str."""
        return str(value)

    @v_args(inline=True)
    def operator(self, token: Token) -> str:
        """Return operator string."""
        return str(token)


class SDFTransformer(SDFBlockTransformer):
    """Transformer that processes the SDF parse tree into data structures."""

    @v_args(inline=True)
    def sdf_file(self, _tag: Token, *items: SDFHeader | None) -> SDFFile:
        """Process the top-level SDF file structure."""
        chunk = self._parsed_chunk(items)
        return SDFFile(header=chunk.header, cells=assemble_cells(chunk.blocks))
