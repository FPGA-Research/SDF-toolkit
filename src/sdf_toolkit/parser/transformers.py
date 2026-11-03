"""SDF parse tree transformer that lowers Lark reductions into the data model.

The parser runs this transformer inline, so each rule method fires as Lark
reduces and no tree is built.  A delay entry reduces before the
``ABSOLUTE``/``INCREMENT`` block and any ``COND`` around it, yet the model
entries are frozen and need both, so the delay rules return a
{class}`_PendingDelay` that the enclosing block finalises.
"""

import re
from collections.abc import Callable, Iterable
from typing import NamedTuple, assert_never

from lark import Token, Transformer, v_args

from sdf_toolkit.core.model import (
    Arrival,
    BidirectSkew,
    CellsDict,
    Cond,
    CondElse,
    DelayEntry,
    DelayMode,
    DelayValues,
    Departure,
    Device,
    Divider,
    EdgeType,
    Entry,
    EntryType,
    Hold,
    Interconnect,
    Iopath,
    Label,
    NetDelay,
    NoChange,
    PathConstraint,
    PathPulse,
    PathPulsePercent,
    Period,
    PeriodConstraint,
    Port,
    PortSpec,
    PulseLimits,
    Recovery,
    Recrem,
    Removal,
    Retain,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    Skew,
    SkewConstraint,
    Slack,
    Timescale,
    TimeUnit,
    TimingCheck,
    TimingEnvEntry,
    TimingPortSpec,
    Values,
    Waveform,
    WaveformEdge,
    Width,
)
from sdf_toolkit.parser.errors import (
    StandardMismatchError,
    StandardViolationError,
    UnsupportedConstructError,
)
from sdf_toolkit.parser.standard import ParseOptions, SDFStandard


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

    kind: EntryType
    build: Callable[[DelayMode, Cond | CondElse | None], DelayEntry]
    cond: Cond | CondElse | None


class _PulseDelval(NamedTuple):
    """A delval ``((delay) (r-limit) [(e-limit)])`` before its list is built."""

    delay: Values
    limits: PulseLimits


# A plain entry, or the entries one COND or CONDELSE wraps.
_DelayDef = _PendingDelay | list[_PendingDelay]

# P1497 Draft 0.10 5.4.9 limits RETAIN to 1 to 3 delvals; OVI SDF 3.0 does not.
_IEEE_RETAIN_MAX = 3

# OVI SDF 3.0 TSVALUE units; IEEE 1497 adds s, ms and fs.
_OVI_TIME_UNITS = frozenset({TimeUnit.US, TimeUnit.NS, TimeUnit.PS})


class _LabelDef(NamedTuple):
    """One ``LABEL`` identifier with its delay, before its mode is known."""

    name: str
    delay: DelayValues


class _Group(NamedTuple):
    """A parenthesised sub-equation, kept apart so a delay ``COND`` can unwrap it."""

    inner: str

    def __str__(self) -> str:
        """Write the group with its parentheses."""
        return f"({self.inner})"


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


# One escape pair of a QSTRING, scanned from the left as the grammar pairs them.
_QSTRING_ESCAPE = re.compile(r"\\[\s\S]")


def remove_quotation(s: str) -> str:
    """Strip the quotes around a QSTRING, keeping its escapes as written.

    Raises
    ------
    ValueError
        If *s* is not wrapped in double quotes.
    """
    if len(s) < 2 or s[0] != '"' or s[-1] != '"':
        raise ValueError(f"Not a QSTRING: {s!r}")
    return s[1:-1]


def _qstring(token: Token | None) -> str | None:
    """Read an optional QSTRING without its quotation marks."""
    return None if token is None else remove_quotation(str(token))


def _delay_values(delvals: Iterable[Values | _PulseDelval]) -> DelayValues:
    """Build a delay value list from its delvals, padded to a defined count.

    Raises
    ------
    SDFModelError
        If there are more than 12 delvals.
    """
    rvalues: list[Values] = []
    limits: list[PulseLimits | None] = []
    for delval in delvals:
        match delval:
            case Values():
                rvalues.append(delval)
                limits.append(None)
            case _PulseDelval(delay=delay, limits=pulse):
                rvalues.append(delay)
                limits.append(pulse)
    has_limits = any(limit is not None for limit in limits)
    return DelayValues.padded(rvalues, limits if has_limits else None)


def _number(token: Token | None) -> float | None:
    """Read an optional FLOAT token of a triple."""
    return None if token is None else float(token)


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
        self._options: ParseOptions | None = None

    def reset(self, options: ParseOptions) -> None:
        """Drop the state of a previous parse and take the rules of the next."""
        self.blocks = []
        self.delays_list = []
        self._options = options

    @property
    def options(self) -> ParseOptions:
        """Return the rules of the current parse.

        Raises
        ------
        RuntimeError
            If no parse has called {meth}`reset` yet.
        """
        if self._options is None:
            raise RuntimeError("The transformer parses only after reset(options)")
        return self._options

    def _violation(
        self, token: Token, problem: str, *, switch: str
    ) -> StandardViolationError:
        """Build the error for *problem* at *token*, naming the *switch* out."""
        line = token.line or 0
        column = token.column or 0
        msg = f"At {line}:{column}, {problem}. Pass {switch} to read it."
        return StandardViolationError(
            msg, line=line, column=column, expected=frozenset()
        )

    def _require_standard(
        self, token: Token, construct: str, *, standard: SDFStandard
    ) -> None:
        """Reject *construct* at *token* unless the rules are *standard*."""
        rules = self.options.rules
        if rules is standard:
            return
        problem = f"{construct} is not part of {rules.full_name}"
        switch = f"--standard {standard} (standard=SDFStandard.{standard.name})"
        raise self._violation(token, problem, switch=switch)

    # ── Top-level structure ──────────────────────────────────────────

    def _parsed_chunk(self, items: tuple[SDFHeader | None, ...]) -> ParsedChunk:
        """Fold the header items of *items* into one header beside the blocks."""
        header = SDFHeader()
        for item in items:
            if item is not None:
                header = header.merged(item)
        return ParsedChunk(header=header, blocks=self.blocks)

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

    # ── Terminals ────────────────────────────────────────────────────

    def QSTRING(self, token: Token) -> Token:  # noqa: N802
        """Reject an escaped double quote outside IEEE 1497.

        Lark calls the method named after a terminal as it shifts the token.
        """
        escapes = _QSTRING_ESCAPE.findall(token, 1, len(token) - 1)
        if '\\"' in escapes:
            self._require_standard(
                token,
                'an escaped double quote \\" in a QSTRING',
                standard=SDFStandard.IEEE_1497,
            )
        return token

    # ── Header fields ────────────────────────────────────────────────

    @v_args(inline=True)
    def sdf_version(self, value: Token) -> SDFHeader:
        """Process SDF version header field.

        Raises
        ------
        StandardMismatchError
            If the parse names a standard and the version names the other.
        """
        version = remove_quotation(str(value))
        asked = self.options.standard
        declared = SDFStandard.declared_by(version)
        if asked is not None and declared is not None and declared is not asked:
            msg = (
                f'SDFVERSION "{version}" at {value.line}:{value.column} names '
                f"{declared.full_name}, but the parse asked for {asked.full_name}. "
                f"Pass --standard {declared} (standard=SDFStandard.{declared.name}), "
                "or name no standard to apply the IEEE 1497 rules without this check."
            )
            raise StandardMismatchError(msg)
        return SDFHeader(sdfversion=version)

    @v_args(inline=True)
    def design(self, value: Token | None = None) -> SDFHeader:
        """Process design name header field."""
        return SDFHeader(
            design=remove_quotation(str(value)) if value is not None else ""
        )

    @v_args(inline=True)
    def date(self, value: Token | None = None) -> SDFHeader:
        """Process date header field."""
        return SDFHeader(date=remove_quotation(str(value)) if value is not None else "")

    @v_args(inline=True)
    def vendor(self, value: Token | None = None) -> SDFHeader:
        """Process vendor header field."""
        return SDFHeader(
            vendor=remove_quotation(str(value)) if value is not None else ""
        )

    @v_args(inline=True)
    def program(self, value: Token | None = None) -> SDFHeader:
        """Process program header field."""
        return SDFHeader(
            program=remove_quotation(str(value)) if value is not None else ""
        )

    @v_args(inline=True)
    def version(self, value: Token | None = None) -> SDFHeader:
        """Process version header field."""
        return SDFHeader(
            version=remove_quotation(str(value)) if value is not None else ""
        )

    @v_args(inline=True)
    def process(self, value: Token | None = None) -> SDFHeader:
        """Process process header field."""
        return SDFHeader(
            process=remove_quotation(str(value)) if value is not None else ""
        )

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
    def timescale(self, number: Token, unit: Token) -> SDFHeader:
        """Process timescale specification.

        Raises
        ------
        ValueError
            If the unit or the number is not one IEEE 1497 allows.
        StandardViolationError
            If the unit is s, ms or fs under the OVI SDF 3.0 rules.
        """
        timescale = Timescale.parse(f"{number}{unit}")
        if timescale.unit not in _OVI_TIME_UNITS:
            self._require_standard(
                unit,
                f"TIMESCALE unit {timescale.unit}",
                standard=SDFStandard.IEEE_1497,
            )
        return SDFHeader(timescale=timescale)

    # ── Value processing ─────────────────────────────────────────────

    @v_args(inline=True)
    def rvalue_number(self, value: Token) -> Values:
        """Process a one-number rvalue, which holds at every corner."""
        return Values.uniform(float(value))

    def empty_rvalue(self, _children: list[object]) -> Values:
        """Process an rvalue ``()`` that gives no value."""
        return Values()

    @v_args(inline=True)
    def real_triple(
        self, min_val: Token, typ_val: Token | None, max_val: Token | None
    ) -> Values:
        """Process a triple that gives its minimum."""
        return Values(min=float(min_val), typ=_number(typ_val), max=_number(max_val))

    @v_args(inline=True)
    def triple_from_typ(self, typ_val: Token, max_val: Token | None) -> Values:
        """Process a triple that omits its minimum and gives its typical value."""
        return Values(typ=float(typ_val), max=_number(max_val))

    @v_args(inline=True)
    def triple_max_only(self, max_val: Token) -> Values:
        """Process a triple that gives only its maximum."""
        return Values(max=float(max_val))

    @v_args(inline=True)
    def pulse_delval(
        self, delay: Values, reject: Values, error: Values | None = None
    ) -> _PulseDelval:
        """Process a delval that groups a delay with its pulse limits."""
        return _PulseDelval(delay=delay, limits=PulseLimits(reject=reject, error=error))

    @v_args(inline=True)
    def delval_list(self, *delvals: Values | _PulseDelval) -> DelayValues:
        """Process a delay value list; its positions are transitions.

        Raises
        ------
        SDFModelError
            If the list holds more than 12 delvals.
        """
        return _delay_values(delvals)

    # ── Cell structure ───────────────────────────────────────────────

    @v_args(inline=True)
    def cell(self, celltype: str, instance: str | None, *_timing_specs: None) -> None:
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

    @v_args(inline=True)
    def include(self, path: Token) -> None:
        """Reject an SDF 1.0 ``INCLUDE``.

        Raises
        ------
        UnsupportedConstructError
            Always, since the parser reads text without a base directory to
            resolve the path against.
        """
        msg = (
            f"INCLUDE {path} at {path.line}:{path.column} is not supported: OVI "
            "dropped INCLUDE after SDF 1.0. Paste the included file in its place."
        )
        raise UnsupportedConstructError(
            msg, line=path.line or 0, column=path.column or 0, expected=frozenset()
        )

    # ── Labels ───────────────────────────────────────────────────────

    @v_args(inline=True)
    def label(self, keyword: Token, *_blocks: None) -> None:
        """Reject a ``LABEL`` outside IEEE 1497; its blocks add the entries."""
        self._require_standard(keyword, "LABEL", standard=SDFStandard.IEEE_1497)

    @v_args(inline=True)
    def label_def(self, name: Token, delay: DelayValues) -> _LabelDef:
        """Process one ``LABEL`` identifier and its delay."""
        return _LabelDef(name=str(name), delay=delay)

    def _labels(self, items: Iterable[_LabelDef], mode: DelayMode) -> None:
        self.delays_list.extend(
            Label(name=item.name, mode=mode, delay=item.delay) for item in items
        )

    @v_args(inline=True)
    def label_absolute(self, *items: _LabelDef) -> None:
        """Process an ``ABSOLUTE`` block of a ``LABEL``."""
        self._labels(items, DelayMode.ABSOLUTE)

    @v_args(inline=True)
    def label_increment(self, *items: _LabelDef) -> None:
        """Process an ``INCREMENT`` block of a ``LABEL``."""
        self._labels(items, DelayMode.INCREMENT)

    # ── Delay blocks ─────────────────────────────────────────────────

    def _finalise(self, items: Iterable[_DelayDef], mode: DelayMode) -> None:
        """Build the pending entries of a delay block with its *mode*."""
        for item in items:
            match item:
                case _PendingDelay(build=build, cond=cond):
                    self.delays_list.append(build(mode, cond))
                case list():
                    self.delays_list.extend(p.build(mode, p.cond) for p in item)
                case _:
                    raise TypeError(f"Unexpected delay definition {item!r}")

    @v_args(inline=True)
    def absolute(self, *items: _DelayDef) -> None:
        """Process absolute delay block."""
        self._finalise(items, DelayMode.ABSOLUTE)

    @v_args(inline=True)
    def increment(self, *items: _DelayDef) -> None:
        """Process increment delay block."""
        self._finalise(items, DelayMode.INCREMENT)

    # ── Delay types ──────────────────────────────────────────────────

    @v_args(inline=True)
    def iopath(
        self,
        input_port: PortSpec,
        output_port: Token,
        *rest: Retain | DelayValues,
    ) -> _PendingDelay:
        """Process IOPATH delay specification with its RETAIN entries."""
        output = PortSpec(port=str(output_port))
        *retains, delay = rest
        if not isinstance(delay, DelayValues):
            raise TypeError(f"Unexpected IOPATH delay {delay!r}")
        retain = tuple(r for r in retains if isinstance(r, Retain))
        if len(retain) != len(retains):
            raise TypeError(f"Unexpected IOPATH RETAIN {retains!r}")
        return _PendingDelay(
            kind=EntryType.IOPATH,
            build=lambda mode, cond: Iopath(
                input=input_port,
                output=output,
                retain=retain,
                delay=delay,
                mode=mode,
                cond=cond,
            ),
            cond=None,
        )

    @v_args(inline=True)
    def retain(self, keyword: Token, *delvals: Values | _PulseDelval) -> Retain:
        """Process an IOPATH RETAIN entry.

        Raises
        ------
        StandardViolationError
            If the entry holds more than 3 delvals under the IEEE 1497 rules.
        SDFModelError
            If the entry holds more than 12 delvals.
        """
        rules = self.options.rules
        if rules is SDFStandard.IEEE_1497 and len(delvals) > _IEEE_RETAIN_MAX:
            other = rules.other
            problem = (
                f"RETAIN takes 1 to {_IEEE_RETAIN_MAX} delvals under "
                f"{rules.full_name}, got {len(delvals)}"
            )
            switch = f"--standard {other} (standard=SDFStandard.{other.name})"
            raise self._violation(keyword, problem, switch=switch)
        return Retain(delay=_delay_values(delvals))

    @v_args(inline=True)
    def netdelay(self, keyword: Token, net: Token, delay: DelayValues) -> _PendingDelay:
        """Process NETDELAY delay specification."""
        self._require_standard(keyword, "NETDELAY", standard=SDFStandard.IEEE_1497)
        return _PendingDelay(
            kind=EntryType.NETDELAY,
            build=lambda mode, cond: NetDelay(
                net=str(net), delay=delay, mode=mode, cond=cond
            ),
            cond=None,
        )

    # ── Pulse propagation ────────────────────────────────────────────

    @staticmethod
    def _pulse_fields(
        args: tuple[Token | Values, ...],
    ) -> tuple[tuple[str, str] | None, Values, Values | None]:
        match args:
            case (Values() as reject,):
                return None, reject, None
            case (Values() as reject, Values() as error):
                return None, reject, error
            case (Token() as source, Token() as sink, Values() as reject):
                return (str(source), str(sink)), reject, None
            case (
                Token() as source,
                Token() as sink,
                Values() as reject,
                Values() as error,
            ):
                return (str(source), str(sink)), reject, error
        raise TypeError(f"Unexpected pulse entry {args!r}")

    @v_args(inline=True)
    def path_pulse(self, *args: Token | Values) -> None:
        """Process a PATHPULSE deltype."""
        path, reject, error = self._pulse_fields(args)
        self.delays_list.append(PathPulse(path=path, reject=reject, error=error))

    @v_args(inline=True)
    def path_pulse_percent(self, *args: Token | Values) -> None:
        """Process a PATHPULSEPERCENT deltype."""
        path, reject, error = self._pulse_fields(args)
        self.delays_list.append(PathPulsePercent(path=path, reject=reject, error=error))

    @v_args(inline=True)
    def interconnect(
        self, source: Token, load: Token, delay: DelayValues
    ) -> _PendingDelay:
        """Process INTERCONNECT delay specification."""
        source_port = PortSpec(port=str(source))
        load_port = PortSpec(port=str(load))
        return _PendingDelay(
            kind=EntryType.INTERCONNECT,
            build=lambda mode, cond: Interconnect(
                source=source_port, load=load_port, delay=delay, mode=mode, cond=cond
            ),
            cond=None,
        )

    @v_args(inline=True)
    def port(self, name: Token, delay: DelayValues) -> _PendingDelay:
        """Process PORT delay specification."""
        port = PortSpec(port=str(name))
        return _PendingDelay(
            kind=EntryType.PORT,
            build=lambda mode, cond: Port(port=port, delay=delay, mode=mode, cond=cond),
            cond=None,
        )

    @v_args(inline=True)
    def device(self, name: Token | None, delay: DelayValues) -> _PendingDelay:
        """Process a DEVICE delay; without a port it covers every output."""
        output = None if name is None else PortSpec(port=str(name))
        return _PendingDelay(
            kind=EntryType.DEVICE,
            build=lambda mode, cond: Device(
                output=output, delay=delay, mode=mode, cond=cond
            ),
            cond=None,
        )

    # ── Port specification ───────────────────────────────────────────

    @v_args(inline=True)
    def port_spec(self, *args: Token | PortSpec) -> PortSpec:
        """Process port specification, keeping the port name as written."""
        match args:
            case (PortSpec() as spec,):
                return spec
            case (Token() as name,):
                return PortSpec(port=str(name))
        raise ValueError(f"Invalid port_spec args: {args}")

    @v_args(inline=True)
    def port_edge(self, edge: Token, name: Token) -> PortSpec:
        """Process a port with its edge identifier."""
        return PortSpec(port=str(name), edge=EdgeType(str(edge).lower()))

    @v_args(inline=True)
    def timing_port(
        self, *args: PortSpec | Token | str | _Group | None
    ) -> TimingPortSpec:
        """Process a timing check port and the ``COND`` it may carry.

        Grammar: ``timing_port: port_spec | "(" COND [QSTRING] equation port_spec ")"``
        """
        match args:
            case (PortSpec(port=port, edge=edge),):
                return TimingPortSpec(port=port, edge=edge)
            case (
                Token(),
                Token() | None as name,
                str() | _Group() as equation,
                PortSpec(port=p, edge=e),
            ):
                cond = Cond(equation=str(equation), name=_qstring(name))
                return TimingPortSpec(port=p, edge=e, cond=cond)
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
        scond: Cond | None,
        ccond: Cond | None,
    ) -> SetupHold:
        """Process setuphold timing check."""
        return SetupHold(
            data=data,
            reference=reference,
            setup=setup,
            hold=hold,
            scond=scond,
            ccond=ccond,
        )

    @v_args(inline=True)
    def recrem_check(
        self,
        data: TimingPortSpec,
        reference: TimingPortSpec,
        recovery: Values,
        removal: Values,
        scond: Cond | None,
        ccond: Cond | None,
    ) -> Recrem:
        """Process a ``RECREM`` check."""
        return Recrem(
            data=data,
            reference=reference,
            recovery=recovery,
            removal=removal,
            scond=scond,
            ccond=ccond,
        )

    @v_args(inline=True)
    def skew_check(
        self, stamp: TimingPortSpec, check: TimingPortSpec, limit: Values
    ) -> Skew:
        """Process a ``SKEW`` check."""
        return Skew(stamp=stamp, check=check, limit=limit)

    @v_args(inline=True)
    def bidirectskew_check(
        self,
        keyword: Token,
        first: TimingPortSpec,
        second: TimingPortSpec,
        first_limit: Values,
        second_limit: Values,
    ) -> BidirectSkew:
        """Process a ``BIDIRECTSKEW`` check, which only IEEE 1497 defines."""
        self._require_standard(keyword, "BIDIRECTSKEW", standard=SDFStandard.IEEE_1497)
        return BidirectSkew(
            first=first,
            second=second,
            first_limit=first_limit,
            second_limit=second_limit,
        )

    @v_args(inline=True)
    def nochange_check(
        self,
        control: TimingPortSpec,
        data: TimingPortSpec,
        setup: Values,
        hold: Values,
    ) -> NoChange:
        """Process a ``NOCHANGE`` check."""
        return NoChange(control=control, data=data, setup=setup, hold=hold)

    @v_args(inline=True)
    def scond(self, name: Token | None, equation: str | _Group) -> Cond:
        """Process the stamp event condition of a check."""
        return Cond(equation=str(equation), name=_qstring(name))

    @v_args(inline=True)
    def ccond(self, name: Token | None, equation: str | _Group) -> Cond:
        """Process the check event condition of a check."""
        return Cond(equation=str(equation), name=_qstring(name))

    @v_args(inline=True)
    def t_check(self, item: TimingCheck) -> TimingCheck:
        """Pass through timing check entry."""
        return item

    @v_args(inline=True)
    def timing_check_list(self, *items: TimingCheck) -> None:
        """Process timing check list."""
        self.delays_list.extend(items)

    # ── Conditional delays ───────────────────────────────────────────

    def _require_one_iopath(
        self, keyword: Token, items: tuple[_PendingDelay, ...]
    ) -> None:
        """Reject a ``COND`` or ``CONDELSE`` that wraps other than one IOPATH."""
        if self.options.loose_cond:
            return
        if [item.kind for item in items] == [EntryType.IOPATH]:
            return
        wrapped = " ".join(str(item.kind).upper() for item in items)
        problem = (
            f"{keyword} wraps {wrapped}, but OVI SDF 3.0 and IEEE 1497 both let "
            "it wrap one IOPATH"
        )
        raise self._violation(keyword, problem, switch="--loose-cond (loose_cond=True)")

    @v_args(inline=True)
    def cond_delay(
        self,
        keyword: Token,
        name: Token | None,
        equation: str | _Group,
        *items: _PendingDelay,
    ) -> list[_PendingDelay]:
        """Attach a ``COND`` to every delay entry it wraps.

        The writer puts the equation of a delay ``COND`` in parentheses, so
        one group spanning the whole equation is unwrapped to keep a round
        trip stable.

        Raises
        ------
        StandardViolationError
            If the ``COND`` wraps other than one IOPATH and the parse does
            not take a loose ``COND``.
        """
        match equation:
            case _Group(inner=inner):
                cond = Cond(equation=inner, name=_qstring(name))
            case str():
                cond = Cond(equation=equation, name=_qstring(name))
            case _:
                raise TypeError(f"Unexpected COND equation {equation!r}")
        self._require_one_iopath(keyword, items)
        return [pending._replace(cond=cond) for pending in items]

    @v_args(inline=True)
    def condelse_delay(
        self, keyword: Token, *items: _PendingDelay
    ) -> list[_PendingDelay]:
        """Mark every delay entry it wraps as the ``CONDELSE`` arc.

        Raises
        ------
        StandardViolationError
            If the ``CONDELSE`` wraps other than one IOPATH and the parse
            does not take a loose ``COND``.
        """
        self._require_one_iopath(keyword, items)
        return [pending._replace(cond=CondElse()) for pending in items]

    @v_args(inline=True)
    def equation(self, *items: Token | _Group) -> str | _Group:
        """Join the tokens of an equation with single spaces.

        An equation that is one parenthesised group returns the group, so the
        caller decides whether the parentheses belong to the text.
        """
        match items:
            case (_Group() as group,):
                return group
        return " ".join(str(item) for item in items)

    @v_args(inline=True)
    def eq_group(self, inner: str | _Group) -> _Group:
        """Process a parenthesised sub-equation."""
        return _Group(inner=str(inner))

    # ── Constraints ──────────────────────────────────────────────────

    @v_args(inline=True)
    def path_constraint(
        self, name: str | None, *rest: PortSpec | Values
    ) -> PathConstraint:
        """Process path constraint.

        Raises
        ------
        ValueError
            If a port carries an edge, which ``PATHCONSTRAINT`` does not allow.
        """
        *specs, rise, fall = rest
        ports: list[str] = []
        for spec in specs:
            match spec:
                case PortSpec(port=port, edge=None):
                    ports.append(port)
                case PortSpec(port=port, edge=edge):
                    msg = f"PATHCONSTRAINT port ({edge} {port}) takes no edge"
                    raise ValueError(msg)
                case Values():
                    raise TypeError(f"Unexpected PATHCONSTRAINT port {spec!r}")
                case _:
                    assert_never(spec)
        if not isinstance(rise, Values) or not isinstance(fall, Values):
            raise TypeError(f"Unexpected PATHCONSTRAINT values {rise!r} {fall!r}")
        return PathConstraint(name=name, ports=tuple(ports), rise=rise, fall=fall)

    @v_args(inline=True)
    def constraint_name(self, name: Token | None) -> str:
        """Process the ``NAME`` of a path constraint; ``(NAME)`` reads as ``""``."""
        return _qstring(name) or ""

    @v_args(inline=True)
    def period_constraint(
        self, port: Token, limit: Values, exception: tuple[str, ...] | None
    ) -> PeriodConstraint:
        """Process a ``PERIODCONSTRAINT``."""
        return PeriodConstraint(port=str(port), limit=limit, exception=exception or ())

    @v_args(inline=True)
    def exception(self, *instances: str | None) -> tuple[str, ...]:
        """Process the instances an ``EXCEPTION`` leaves out."""
        return tuple("" if name is None else name for name in instances)

    @v_args(inline=True)
    def skew_constraint(self, port: PortSpec, limit: Values) -> SkewConstraint:
        """Process a ``SKEWCONSTRAINT``."""
        return SkewConstraint(port=port, limit=limit)

    @v_args(inline=True)
    def arrival(
        self,
        reference: PortSpec | None,
        port: Token,
        earliest_rise: Values,
        latest_rise: Values,
        earliest_fall: Values,
        latest_fall: Values,
    ) -> Arrival:
        """Process an ``ARRIVAL``."""
        return Arrival(
            reference=reference,
            port=str(port),
            earliest_rise=earliest_rise,
            latest_rise=latest_rise,
            earliest_fall=earliest_fall,
            latest_fall=latest_fall,
        )

    @v_args(inline=True)
    def departure(
        self,
        reference: PortSpec | None,
        port: Token,
        earliest_rise: Values,
        latest_rise: Values,
        earliest_fall: Values,
        latest_fall: Values,
    ) -> Departure:
        """Process a ``DEPARTURE``."""
        return Departure(
            reference=reference,
            port=str(port),
            earliest_rise=earliest_rise,
            latest_rise=latest_rise,
            earliest_fall=earliest_fall,
            latest_fall=latest_fall,
        )

    @v_args(inline=True)
    def slack(
        self,
        port: Token,
        rise_setup: Values,
        fall_setup: Values,
        rise_hold: Values,
        fall_hold: Values,
        period: Token | None,
    ) -> Slack:
        """Process a ``SLACK`` and its optional clock period."""
        return Slack(
            port=str(port),
            rise_setup=rise_setup,
            fall_setup=fall_setup,
            rise_hold=rise_hold,
            fall_hold=fall_hold,
            period=_number(period),
        )

    @v_args(inline=True)
    def waveform(self, port: Token, period: Token, *edges: WaveformEdge) -> Waveform:
        """Process a ``WAVEFORM``."""
        return Waveform(port=str(port), period=float(period), edges=edges)

    @v_args(inline=True)
    def wave_edge(self, edge: Token, *offsets: Token) -> WaveformEdge:
        """Process one edge of a ``WAVEFORM``."""
        return WaveformEdge(
            edge=EdgeType(str(edge).lower()),
            offsets=tuple(float(o) for o in offsets),
        )

    @v_args(inline=True)
    def constraints_list(self, *items: TimingEnvEntry) -> None:
        """Process constraints list."""
        self.delays_list.extend(items)


class SDFTransformer(SDFBlockTransformer):
    """Transformer that processes the SDF parse tree into data structures."""

    @v_args(inline=True)
    def sdf_file(self, _tag: Token, *items: SDFHeader | None) -> SDFFile:
        """Process the top-level SDF file structure."""
        chunk = self._parsed_chunk(items)
        return SDFFile(header=chunk.header, cells=assemble_cells(chunk.blocks))
