"""Verilog SDF back-annotation via specify blocks and net delays.

Yosys reads the Verilog and supplies every structural fact: modules, ports,
cells, nets and the byte span of each in the source.  Every text edit lands at
a position derived from those spans.  Yosys drops specify blocks and keyword
positions, so a comment-aware tokenizer runs inside a Yosys-reported module
span to find the ``endmodule`` keyword, an existing ``specify`` block and the
shape of a net declaration.  The tokenizer never decides which module, net or
cell an edit targets.

{mod}`sdf_toolkit.io.yosys` reads the netlist and {mod}`sdf_toolkit.io.specify`
builds the specify items.  Any SDF content without a faithful Verilog form
raises {class}`~sdf_toolkit.io.yosys.AnnotationError` naming the entry,
rather than being dropped.
"""

import bisect
import string
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from sdf_toolkit.core.delays import (
    Interconnect,
)
from sdf_toolkit.core.enums import Corner, Divider, EdgeSelect
from sdf_toolkit.core.sdffile import Entry, SDFFile
from sdf_toolkit.io.specify import (
    SdfPin,
    _mintypmax,
    entries_to_specify,
    render_specify_block,
    require_absolute,
    select_worst_case_delays,
)
from sdf_toolkit.io.yosys import (
    AnnotationError,
    SourceSpan,
    YosysCell,
    YosysDesign,
    YosysModule,
    YosysNet,
    parse_yosys_json,
    yosys_running,
)
from sdf_toolkit.parser.standard import SDFStandard


@dataclass(frozen=True, slots=True)
class WireDelay:
    """A net delay for the declaration of ``net`` in ``module``.

    ``delays`` holds 1 to 3 ``min:typ:max`` values in Verilog net-delay
    order: rise, fall, turn-off.
    """

    module: str
    net: str
    delays: tuple[str, ...]


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
