"""Verilog SDF back-annotation via specify blocks and wire delays.

Embeds SDF timing data directly into Verilog cell library modules as
``specify`` blocks and annotates ``wire #(delay)`` for INTERCONNECT delays.
Requires Yosys for robust Verilog parsing.
"""

import json
import re
import subprocess
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path  # noqa: TC003 - used at runtime in signatures

import jinja2

from sdf_toolkit.core.model import (
    ArcKey,
    Cond,
    CondElse,
    Corner,
    DelayValues,
    Device,
    Divider,
    EdgeSelect,
    Entry,
    Hold,
    Interconnect,
    Iopath,
    MissingDelayError,
    PathConstraint,
    Period,
    Port,
    PortSpec,
    Recovery,
    Removal,
    SDFFile,
    Setup,
    SetupHold,
    TimingPortSpec,
    Values,
    Width,
)


class SpecifyKind(StrEnum):
    """Kind of entry in a Verilog specify block."""

    IOPATH = "iopath"
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


# ── Yosys JSON data structures ──────────────────────────────────────


@dataclass
class YosysPort:
    """A port in a Yosys module."""

    name: str
    direction: PortDirection
    bits: list[int | str] = field(default_factory=list)


@dataclass
class YosysCell:
    """A cell instance in a Yosys module."""

    name: str
    cell_type: str
    connections: dict[str, list[int | str]] = field(default_factory=dict)


@dataclass
class YosysModule:
    """A module parsed from Yosys JSON output."""

    name: str
    ports: dict[str, YosysPort] = field(default_factory=dict)
    cells: dict[str, YosysCell] = field(default_factory=dict)
    netnames: dict[str, list[int | str]] = field(default_factory=dict)


@dataclass
class YosysDesign:
    """Top-level Yosys design containing multiple modules."""

    modules: dict[str, YosysModule] = field(default_factory=dict)


# ── Specify block data structures ───────────────────────────────────


@dataclass
class SpecifyEntry:
    """A single entry in a Verilog specify block.

    For a path, ``from_pin`` is the input and ``to_pin`` the output.  For a
    two-port check, ``from_pin`` is the data port (SDF's first port) and
    ``to_pin`` the reference port; the formatter puts them in the argument
    order each Verilog system task takes.  A one-port check uses
    ``from_pin`` only.
    """

    kind: SpecifyKind
    from_pin: str
    to_pin: str | None = None
    from_edge: str | None = None  # "posedge" | "negedge" | None
    to_edge: str | None = None
    # Path delay triples in SDF rvalue order, which Verilog also uses.
    delays: list[str] = field(default_factory=list)
    limit: str | None = None
    # A path condition: an equation for ``if``, "" for ``ifnone``.
    condition: str | None = None
    # Timing-check conditions, written after ``&&&`` on their event.
    from_condition: str | None = None
    to_condition: str | None = None
    setup_limit: str | None = None
    hold_limit: str | None = None


@dataclass
class WireDelay:
    """A delay annotation for a wire declaration.

    ``delays`` holds 1 to 3 triples in Verilog net-delay order: rise, fall,
    turn-off.
    """

    net_name: str
    delays: list[str]


# ── Yosys integration ───────────────────────────────────────────────


def run_yosys(verilog_path: Path) -> dict:
    """Run Yosys to parse a Verilog file and return JSON representation.

    Parameters
    ----------
    verilog_path : Path
        Path to the Verilog file to parse.

    Returns
    -------
    dict
        Parsed Yosys JSON output.

    Raises
    ------
    FileNotFoundError
        If Yosys is not installed or not in PATH.
    RuntimeError
        If Yosys returns a non-zero exit code.
    """
    verilog_abs = str(verilog_path.resolve())
    cmd = [
        "yosys",
        "-q",
        "-p",
        f"read_verilog {verilog_abs}; write_json -",
    ]
    try:
        result = subprocess.run(  # noqa: S603
            cmd,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        msg = (
            "Yosys is required for Verilog annotation but was not found. "
            "Install it from https://github.com/YosysHQ/yosys"
        )
        raise FileNotFoundError(msg) from None

    if result.returncode != 0:
        msg = f"Yosys failed (exit {result.returncode}):\n{result.stderr}"
        raise RuntimeError(msg)

    return json.loads(result.stdout)


def parse_yosys_json(json_data: dict) -> YosysDesign:
    """Parse Yosys JSON output into a YosysDesign.

    Parameters
    ----------
    json_data : dict
        Raw JSON output from ``yosys -p "write_json -"``.

    Returns
    -------
    YosysDesign
        Parsed design with modules, ports, cells, and netnames.
    """
    design = YosysDesign()
    for mod_name, mod_data in json_data.get("modules", {}).items():
        module = YosysModule(name=mod_name)

        # Parse ports
        for port_name, port_data in mod_data.get("ports", {}).items():
            module.ports[port_name] = YosysPort(
                name=port_name,
                direction=PortDirection(port_data.get("direction", "input")),
                bits=port_data.get("bits", []),
            )

        # Parse cells
        for cell_name, cell_data in mod_data.get("cells", {}).items():
            module.cells[cell_name] = YosysCell(
                name=cell_name,
                cell_type=cell_data.get("type", ""),
                connections=cell_data.get("connections", {}),
            )

        # Parse netnames
        for net_name, net_data in mod_data.get("netnames", {}).items():
            module.netnames[net_name] = net_data.get("bits", [])

        design.modules[mod_name] = module

    return design


def build_bit_to_net_map(module: YosysModule) -> dict[int, str]:
    """Build a reverse mapping from bit index to net name.

    Parameters
    ----------
    module : YosysModule
        A parsed Yosys module.

    Returns
    -------
    dict[int, str]
        Mapping from bit index to net name.
    """
    bit_map: dict[int, str] = {}
    for net_name, bits in module.netnames.items():
        for bit in bits:
            if isinstance(bit, int):
                bit_map[bit] = net_name
    return bit_map


# ── SDF-to-module matching ──────────────────────────────────────────


def match_sdf_to_modules(sdf: SDFFile, design: YosysDesign) -> dict[str, list[Entry]]:
    """Match SDF cell types to Yosys modules and collect entries.

    For each cell type in the SDF that has a corresponding module in the
    Yosys design, collect ALL entries across ALL instances of that cell type.

    Parameters
    ----------
    sdf : SDFFile
        Parsed SDF file.
    design : YosysDesign
        Parsed Yosys design.

    Returns
    -------
    dict[str, list[Entry]]
        Mapping from module name to list of all entries.
    """
    matched: dict[str, list[Entry]] = {}
    for cell_type, instances in sdf.cells.items():
        if cell_type not in design.modules:
            continue
        entries = [
            entry for inst_entries in instances.values() for entry in inst_entries
        ]
        if entries:
            matched[cell_type] = entries
    return matched


def _entry_scalar(entry: Entry, corner: Corner, edge: EdgeSelect) -> float | None:
    """Reduce *entry* to the number that ranks it, or None without a value.

    A delay ranks by its scalar, a check by its limit at *corner*, and a
    SETUPHOLD by the width of its window, setup plus hold.
    """
    match entry:
        case Iopath() | Port() | Interconnect() | Device():
            try:
                return entry.delay.scalar(corner=corner, edge=edge)
            except MissingDelayError:
                return None
        case Setup() | Hold() | Recovery() | Removal() | Width() | Period():
            return entry.limit.at(corner)
        case SetupHold():
            setup, hold = entry.setup.at(corner), entry.hold.at(corner)
            return None if setup is None or hold is None else setup + hold
        case PathConstraint():
            return None


def select_worst_case_delays(
    entries: list[Entry],
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> list[Entry]:
    """Select the worst-case entry per arc key.

    Groups entries by ``entry.key`` and keeps the entry with the largest
    scalar at *corner*; an entry without a value loses to one with a value.

    Parameters
    ----------
    entries : list[Entry]
        All entries for a given module.
    corner : Corner
        Process corner to rank by.
    edge : EdgeSelect
        How delay rvalues reduce to one number.

    Returns
    -------
    list[Entry]
        One entry per arc key, in order of first appearance.
    """
    groups: dict[ArcKey, tuple[Entry, float | None]] = {}
    for entry in entries:
        scalar = _entry_scalar(entry, corner, edge)
        existing = groups.get(entry.key)
        if existing is None or (
            scalar is not None and (existing[1] is None or scalar > existing[1])
        ):
            groups[entry.key] = (entry, scalar)
    return [entry for entry, _ in groups.values()]


# ── Specify block generation ────────────────────────────────────────


def _format_values_triple(v: Values) -> str:
    """Format a Values object as a Verilog delay triple ``min:typ:max``."""

    def _fmt(val: float | None) -> str:
        if val is None:
            return ""
        if val == int(val):
            return str(int(val))
        return str(val)

    return f"{_fmt(v.min)}:{_fmt(v.typ)}:{_fmt(v.max)}"


def _format_delays(delay: DelayValues) -> list[str]:
    """Format every rvalue of *delay* as a triple, in SDF order."""
    return [_format_values_triple(v) for v in delay.rvalues]


def _format_pin(pin: str, edge: str | None) -> str:
    """Format a pin with optional edge specifier."""
    if edge:
        return f"{edge} {pin}"
    return pin


def _edge_val(port: PortSpec | TimingPortSpec) -> str | None:
    """Return the port's edge keyword, or None."""
    return port.edge.value if port.edge else None


def _cond_val(port: TimingPortSpec) -> str | None:
    """Return the port's timing-check condition equation, or None."""
    return port.cond.equation if port.cond else None


def _path_condition(cond: Cond | CondElse | None) -> str | None:
    """Return the specify-path condition: an equation, "" for ifnone, or None."""
    match cond:
        case Cond():
            return cond.equation
        case CondElse():
            return ""
        case None:
            return None


_LIMIT_CHECK_KINDS: dict[type, SpecifyKind] = {
    Setup: SpecifyKind.SETUP,
    Hold: SpecifyKind.HOLD,
    Recovery: SpecifyKind.RECOVERY,
    Removal: SpecifyKind.REMOVAL,
}


def entries_to_specify(
    entries: list[Entry],
) -> list[SpecifyEntry]:
    """Convert SDF entries to SpecifyEntry objects.

    Entries a specify block cannot hold (PORT, INTERCONNECT, DEVICE and
    PATHCONSTRAINT) are skipped.

    Parameters
    ----------
    entries : list[Entry]
        Filtered/worst-case SDF entries for a module.

    Returns
    -------
    list[SpecifyEntry]
        Specify block entries ready for rendering.
    """
    result: list[SpecifyEntry] = []
    for entry in entries:
        match entry:
            case Iopath():
                result.append(
                    SpecifyEntry(
                        kind=SpecifyKind.IOPATH,
                        from_pin=entry.input.port,
                        to_pin=entry.output.port,
                        from_edge=_edge_val(entry.input),
                        to_edge=_edge_val(entry.output),
                        delays=_format_delays(entry.delay),
                        condition=_path_condition(entry.cond),
                    )
                )
            case SetupHold():
                result.append(
                    SpecifyEntry(
                        kind=SpecifyKind.SETUPHOLD,
                        from_pin=entry.data.port,
                        to_pin=entry.reference.port,
                        from_edge=_edge_val(entry.data),
                        to_edge=_edge_val(entry.reference),
                        from_condition=_cond_val(entry.data),
                        to_condition=_cond_val(entry.reference),
                        setup_limit=_format_values_triple(entry.setup),
                        hold_limit=_format_values_triple(entry.hold),
                    )
                )
            case Setup() | Hold() | Recovery() | Removal():
                result.append(
                    SpecifyEntry(
                        kind=_LIMIT_CHECK_KINDS[type(entry)],
                        from_pin=entry.data.port,
                        to_pin=entry.reference.port,
                        from_edge=_edge_val(entry.data),
                        to_edge=_edge_val(entry.reference),
                        from_condition=_cond_val(entry.data),
                        to_condition=_cond_val(entry.reference),
                        limit=_format_values_triple(entry.limit),
                    )
                )
            case Width() | Period():
                result.append(
                    SpecifyEntry(
                        kind=(
                            SpecifyKind.WIDTH
                            if isinstance(entry, Width)
                            else SpecifyKind.PERIOD
                        ),
                        from_pin=entry.port.port,
                        from_edge=_edge_val(entry.port),
                        from_condition=_cond_val(entry.port),
                        limit=_format_values_triple(entry.limit),
                    )
                )
            case Port() | Interconnect() | Device() | PathConstraint():
                continue

    return result


def _format_event(pin: str | None, edge: str | None, condition: str | None) -> str:
    """Format a timing-check event: ``[edge] pin [&&& condition]``."""
    if pin is None:
        msg = "A two-port timing check needs both ports"
        raise ValueError(msg)
    event = _format_pin(pin, edge)
    return f"{event} &&& {condition}" if condition else event


def _format_specify_entry(entry: SpecifyEntry) -> str:
    """Format a single SpecifyEntry as a Verilog specify statement line.

    Two-port checks take their Verilog argument order (IEEE 1364-2005 §15):
    ``$setup(data, reference, limit)``, ``$hold(reference, data, limit)`` and
    ``$setuphold(reference, data, setup, hold)``.
    """
    indent = "        "
    data_str = _format_event(entry.from_pin, entry.from_edge, entry.from_condition)

    match entry.kind:
        case SpecifyKind.IOPATH:
            src = _format_pin(entry.from_pin, entry.from_edge)
            dst = _format_pin(entry.to_pin or "", entry.to_edge)
            path_str = f"({src} => {dst}) = ({', '.join(entry.delays)})"
            if entry.condition == "":
                return f"{indent}ifnone {path_str};"
            if entry.condition:
                return f"{indent}if ({entry.condition}) {path_str};"
            return f"{indent}{path_str};"
        case SpecifyKind.WIDTH | SpecifyKind.PERIOD:
            return f"{indent}${entry.kind}({data_str}, {entry.limit});"
        case _:
            pass

    ref_str = _format_event(entry.to_pin, entry.to_edge, entry.to_condition)
    match entry.kind:
        case SpecifyKind.SETUP:
            return f"{indent}$setup({data_str}, {ref_str}, {entry.limit});"
        case SpecifyKind.HOLD:
            return f"{indent}$hold({ref_str}, {data_str}, {entry.limit});"
        case SpecifyKind.SETUPHOLD:
            s, h = entry.setup_limit, entry.hold_limit
            return f"{indent}$setuphold({ref_str}, {data_str}, {s}, {h});"
        case SpecifyKind.RECOVERY | SpecifyKind.REMOVAL:
            # SDF port order, as earlier releases wrote it.
            return f"{indent}${entry.kind}({data_str}, {ref_str}, {entry.limit});"
    msg = f"Unhandled specify kind {entry.kind}"
    raise ValueError(msg)


_jinja_env = jinja2.Environment(
    loader=jinja2.PackageLoader("sdf_toolkit.io", "templates"),
    keep_trailing_newline=True,
)


def render_specify_block(entries: list[SpecifyEntry]) -> str:
    """Render a list of SpecifyEntry objects as a Verilog specify block.

    Parameters
    ----------
    entries : list[SpecifyEntry]
        The specify entries to render.

    Returns
    -------
    str
        A complete ``specify ... endspecify`` block.
    """
    lines = [_format_specify_entry(e) for e in entries]
    template = _jinja_env.get_template("specify.j2")
    return template.render(lines=lines)


# ── INTERCONNECT --> Wire delays ────────────────────────────────────


def resolve_interconnects(
    entries: list[Entry],
    design: YosysDesign,
    *,
    top_module: str,
    sdf_divider: Divider,
) -> list[WireDelay]:
    """Resolve INTERCONNECT entries to wire delay annotations.

    Parameters
    ----------
    entries : list[Entry]
        All entries from the top-level SDF cell (typically INTERCONNECT).
    design : YosysDesign
        Parsed Yosys design.
    top_module : str
        Name of the top-level module in the Yosys design.
    sdf_divider : Divider
        Hierarchy divider in force for the SDF file.

    Returns
    -------
    list[WireDelay]
        Wire delay annotations for net declarations.

    Raises
    ------
    ValueError
        If an INTERCONNECT delay has more than 3 rvalues, which a Verilog
        net delay cannot hold.
    """
    if top_module not in design.modules:
        return []
    module = design.modules[top_module]
    bit_map = build_bit_to_net_map(module)

    wire_delays: list[WireDelay] = []
    for entry in entries:
        if not isinstance(entry, Interconnect):
            continue

        # Split the load on the last divider to get (instance_path, pin_name)
        load = entry.load.port
        divider_pos = load.rfind(sdf_divider)
        if divider_pos < 0:
            continue
        instance_path = load[:divider_pos]
        pin_name = load[divider_pos + 1 :]

        # Find instance in top module cells
        cell = module.cells.get(instance_path)
        if cell is None:
            continue

        # Get bit index from cell connections
        conn_bits = cell.connections.get(pin_name)
        if not conn_bits:
            continue

        if len(entry.delay.rvalues) > 3:
            msg = (
                f"INTERCONNECT {entry.source.port} {load} has "
                f"{len(entry.delay.rvalues)} rvalues; a Verilog net delay takes "
                "at most 3 (rise, fall, turn-off)"
            )
            raise ValueError(msg)

        # Look up net name from bit index
        for bit in conn_bits:
            if isinstance(bit, int) and bit in bit_map:
                wire_delays.append(
                    WireDelay(net_name=bit_map[bit], delays=_format_delays(entry.delay))
                )
                break

    return wire_delays


# ── Text insertion ──────────────────────────────────────────────────


def insert_specify_blocks(verilog_text: str, module_blocks: dict[str, str]) -> str:
    """Insert specify blocks into Verilog module definitions.

    Scans the Verilog text line-by-line, tracking the current module name.
    When hitting ``endmodule``, inserts the corresponding specify block
    before it. Skips modules that already contain a ``specify`` block.

    Parameters
    ----------
    verilog_text : str
        Original Verilog source text.
    module_blocks : dict[str, str]
        Mapping from module name to rendered specify block text.

    Returns
    -------
    str
        Modified Verilog text with specify blocks inserted.
    """
    lines = verilog_text.split("\n")
    result: list[str] = []
    current_module: str | None = None
    has_specify = False

    for line in lines:
        stripped = line.strip()

        # Track current module
        if stripped.startswith("module "):
            # Module name ends at whitespace, '(', or ';'
            current_module = re.split(r"[\s(;]", stripped[7:], maxsplit=1)[0]
            has_specify = False

        # Track if module already has specify
        if stripped == "specify":
            has_specify = True

        # Insert before endmodule
        if stripped == "endmodule" and current_module and not has_specify:
            block = module_blocks.get(current_module)
            if block:
                result.append(block)
            current_module = None

        result.append(line)

    return "\n".join(result)


def insert_wire_delays(verilog_text: str, wire_delays: list[WireDelay]) -> str:
    """Insert delay annotations into wire declarations.

    For each ``wire`` declaration matching a net name in the delay list,
    inserts ``#(rise, fall, turn-off)`` after the ``wire`` keyword, with as
    many values as the delay has.

    Parameters
    ----------
    verilog_text : str
        Verilog source text (possibly already with specify blocks).
    wire_delays : list[WireDelay]
        Wire delay annotations to insert.

    Returns
    -------
    str
        Modified Verilog text with wire delays annotated.
    """
    if not wire_delays:
        return verilog_text

    delay_map: dict[str, WireDelay] = {wd.net_name: wd for wd in wire_delays}
    lines = verilog_text.split("\n")
    result: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("wire ") and "#" not in stripped:
            # Check if this wire declaration matches a known net
            for net_name, wd in delay_map.items():
                if net_name in stripped:
                    delay_str = f"#({', '.join(wd.delays)})"
                    line = line.replace("wire ", f"wire {delay_str} ", 1)
                    break
        result.append(line)

    return "\n".join(result)


# ── Orchestrator ────────────────────────────────────────────────────


def annotate_verilog(
    sdf_path: Path,
    verilog_path: Path,
    *,
    output_path: Path | None = None,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> str:
    """Annotate a Verilog cell library with SDF timing data.

    Parses the SDF file, uses Yosys to parse the Verilog file, matches
    SDF cell types to Verilog modules, generates specify blocks, and
    inserts them into the Verilog text.

    Parameters
    ----------
    sdf_path : Path
        Path to the SDF timing file.
    verilog_path : Path
        Path to the Verilog cell library file.
    output_path : Path or None
        If provided, write annotated Verilog to this path.
    corner : Corner
        Process corner for worst-case selection.
    edge : EdgeSelect
        How delay rvalues reduce to one number for worst-case selection.

    Returns
    -------
    str
        The annotated Verilog text.
    """
    from sdf_toolkit.parser.parser import parse_sdf

    # 1. Parse SDF
    sdf = parse_sdf(sdf_path.read_text())

    # 2. Parse Verilog via Yosys
    yosys_json = run_yosys(verilog_path)
    design = parse_yosys_json(yosys_json)

    # 3. Match SDF cells to modules
    matched = match_sdf_to_modules(sdf, design)

    # 4. Generate specify blocks per module
    module_blocks: dict[str, str] = {}
    for mod_name, entries in matched.items():
        worst = select_worst_case_delays(entries, corner=corner, edge=edge)
        specify_entries = entries_to_specify(worst)
        if specify_entries:
            module_blocks[mod_name] = render_specify_block(specify_entries)

    # 5. Resolve INTERCONNECT entries from every cell
    top_entries: list[Entry] = [
        entry
        for instances in sdf.cells.values()
        for inst_entries in instances.values()
        for entry in inst_entries
        if isinstance(entry, Interconnect)
    ]

    wire_delays = resolve_interconnects(
        top_entries,
        design,
        top_module=sdf.header.design or "",
        sdf_divider=sdf.header.effective_divider,
    )

    # 6. Insert into Verilog text
    verilog_text = verilog_path.read_text()
    verilog_text = insert_specify_blocks(verilog_text, module_blocks)
    verilog_text = insert_wire_delays(verilog_text, wire_delays)

    # 7. Write output
    if output_path is not None:
        output_path.write_text(verilog_text)

    return verilog_text
