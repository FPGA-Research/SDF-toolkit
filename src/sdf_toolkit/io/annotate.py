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
    BaseEntry,
    DelayField,
    DelayFieldLike,
    DelayMetric,
    DelayMetricLike,
    DelayPaths,
    EdgeType,
    EntryType,
    SDFFile,
    Values,
)


class SpecifyKind(StrEnum):
    """Kind of entry in a Verilog specify block."""

    IOPATH = "iopath"
    SETUP = "setup"
    HOLD = "hold"
    SETUPHOLD = "setuphold"
    WIDTH = "width"
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
    netnames: dict[str, list[int]] = field(default_factory=dict)


@dataclass
class YosysDesign:
    """Top-level Yosys design containing multiple modules."""

    modules: dict[str, YosysModule] = field(default_factory=dict)


# ── Specify block data structures ───────────────────────────────────


@dataclass
class SpecifyEntry:
    """A single entry in a Verilog specify block."""

    kind: SpecifyKind
    from_pin: str
    to_pin: str | None = None
    from_edge: str | None = None  # "posedge" | "negedge" | None
    to_edge: str | None = None
    rise_delay: str = ""
    fall_delay: str | None = None
    condition: str | None = None
    setup_limit: str | None = None
    hold_limit: str | None = None


@dataclass
class WireDelay:
    """A delay annotation for a wire declaration."""

    net_name: str
    rise_delay: str
    fall_delay: str | None = None


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


def match_sdf_to_modules(
    sdf: SDFFile, design: YosysDesign
) -> dict[str, list[BaseEntry]]:
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
    dict[str, list[BaseEntry]]
        Mapping from module name to list of all entries.
    """
    matched: dict[str, list[BaseEntry]] = {}
    for cell_type, instances in sdf.cells.items():
        if cell_type not in design.modules:
            continue
        entries: list[BaseEntry] = []
        for _inst_name, inst_entries in instances.items():
            entries.extend(inst_entries.values())
        if entries:
            matched[cell_type] = entries
    return matched


def select_worst_case_delays(
    entries: list[BaseEntry],
    field_name: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
) -> list[BaseEntry]:
    """Select the worst-case entry per unique key.

    Groups entries by ``(type, from_pin, to_pin, from_pin_edge, to_pin_edge,
    is_cond, cond_equation)`` and keeps the entry with the largest scalar
    delay for the given field and metric.

    Parameters
    ----------
    entries : list[BaseEntry]
        All entries for a given module.
    field_name : str
        Delay field name (e.g. "slow", "nominal", "fast").
    metric : str
        Metric name (e.g. "min", "avg", "max").

    Returns
    -------
    list[BaseEntry]
        One entry per unique key, with the worst-case delay.
    """
    groups: dict[tuple, BaseEntry] = {}
    for entry in entries:
        key = (
            entry.type,
            entry.from_pin,
            entry.to_pin,
            entry.from_pin_edge,
            entry.to_pin_edge,
            entry.is_cond,
            entry.cond_equation,
        )
        if key not in groups:
            groups[key] = entry
        else:
            existing = groups[key]
            existing_scalar = (
                existing.delay_paths.get_scalar(field_name, metric)
                if existing.delay_paths
                else None
            )
            new_scalar = (
                entry.delay_paths.get_scalar(field_name, metric)
                if entry.delay_paths
                else None
            )
            if new_scalar is not None and (
                existing_scalar is None or new_scalar > existing_scalar
            ):
                groups[key] = entry
    return list(groups.values())


# ── Specify block generation ────────────────────────────────────────


def _format_values_triple(v: Values) -> str:
    """Format a Values object as a Verilog delay triple ``min:typ:max``."""

    def _fmt(val: float | None) -> str:
        if val is None:
            return ""
        if val == int(val):
            return str(int(val))
        return str(val)

    return f"{_fmt(v.min)}:{_fmt(v.avg)}:{_fmt(v.max)}"


def _format_pin(pin: str, edge: str | None) -> str:
    """Format a pin with optional edge specifier."""
    if edge:
        return f"{edge} {pin}"
    return pin


def _edge_val(edge: EdgeType | None) -> str | None:
    """Extract edge string value, or None."""
    return edge.value if edge else None


def entries_to_specify(
    entries: list[BaseEntry],
) -> list[SpecifyEntry]:
    """Convert SDF BaseEntry objects to SpecifyEntry objects.

    Parameters
    ----------
    entries : list[BaseEntry]
        Filtered/worst-case SDF entries for a module.

    Returns
    -------
    list[SpecifyEntry]
        Specify block entries ready for rendering.
    """
    # Timing check kinds that use a single delay limit
    single_limit_kinds: dict[EntryType, SpecifyKind] = {
        EntryType.SETUP: SpecifyKind.SETUP,
        EntryType.HOLD: SpecifyKind.HOLD,
        EntryType.WIDTH: SpecifyKind.WIDTH,
        EntryType.RECOVERY: SpecifyKind.RECOVERY,
        EntryType.REMOVAL: SpecifyKind.REMOVAL,
    }

    result: list[SpecifyEntry] = []
    for entry in entries:
        dp = entry.delay_paths
        if dp is None:
            continue

        from_edge = _edge_val(entry.from_pin_edge)
        to_edge = _edge_val(entry.to_pin_edge)

        if entry.type == EntryType.IOPATH:
            rise_str, fall_str = _extract_rise_fall(dp)
            cond = entry.cond_equation if entry.is_cond else None
            result.append(
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin=entry.from_pin or "",
                    to_pin=entry.to_pin,
                    from_edge=from_edge,
                    to_edge=to_edge,
                    rise_delay=rise_str,
                    fall_delay=fall_str,
                    condition=cond,
                )
            )
        elif entry.type == EntryType.SETUPHOLD:
            s = _format_values_triple(dp.setup) if dp.setup else "0"
            h = _format_values_triple(dp.hold) if dp.hold else "0"
            result.append(
                SpecifyEntry(
                    kind=SpecifyKind.SETUPHOLD,
                    from_pin=entry.from_pin or "",
                    to_pin=entry.to_pin,
                    from_edge=from_edge,
                    to_edge=to_edge,
                    setup_limit=s,
                    hold_limit=h,
                )
            )
        elif entry.type in single_limit_kinds:
            limit = _extract_single_delay(dp)
            result.append(
                SpecifyEntry(
                    kind=single_limit_kinds[entry.type],
                    from_pin=entry.from_pin or "",
                    to_pin=entry.to_pin,
                    from_edge=from_edge,
                    to_edge=to_edge,
                    rise_delay=limit,
                )
            )

    return result


def _extract_rise_fall(dp: DelayPaths) -> tuple[str, str | None]:
    """Extract rise and fall delay strings from a DelayPaths.

    With fast/slow, returns ``(fast_triple, slow_triple)``.
    With nominal only, returns ``(nominal_triple, None)``.
    """
    if dp.fast is not None and dp.slow is not None:
        return _format_values_triple(dp.fast), _format_values_triple(dp.slow)
    if dp.nominal is not None:
        return _format_values_triple(dp.nominal), None
    return "0", None


def _extract_single_delay(dp: DelayPaths) -> str:
    """Extract a single delay string from a DelayPaths (for timing checks)."""
    for values in (dp.nominal, dp.setup, dp.fast, dp.slow):
        if values is not None:
            return _format_values_triple(values)
    return "0"


def _format_specify_entry(entry: SpecifyEntry) -> str:
    """Format a single SpecifyEntry as a Verilog specify statement line."""
    indent = "        "
    data_str = _format_pin(entry.from_pin, entry.from_edge)
    ref_str = _format_pin(entry.to_pin or "", entry.to_edge)

    if entry.kind == SpecifyKind.IOPATH:
        if entry.fall_delay is not None:
            delay_str = f"({entry.rise_delay}, {entry.fall_delay})"
        else:
            delay_str = f"({entry.rise_delay})"
        path_str = f"({data_str} => {ref_str}) = {delay_str}"
        if entry.condition:
            return f"{indent}if ({entry.condition}) {path_str};"
        return f"{indent}{path_str};"

    if entry.kind == SpecifyKind.SETUP:
        return f"{indent}$setup({data_str}, {ref_str}, {entry.rise_delay});"

    if entry.kind == SpecifyKind.SETUPHOLD:
        s, h = entry.setup_limit, entry.hold_limit
        return f"{indent}$setuphold({ref_str}, {data_str}, {s}, {h});"

    if entry.kind == SpecifyKind.WIDTH:
        return f"{indent}$width({data_str}, {entry.rise_delay});"

    # hold, recovery, removal all share: $kind(ref, data, limit)
    if entry.kind in (SpecifyKind.HOLD, SpecifyKind.RECOVERY, SpecifyKind.REMOVAL):
        return f"{indent}${entry.kind}({ref_str}, {data_str}, {entry.rise_delay});"

    return ""


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
    entries: list[BaseEntry],
    design: YosysDesign,
    top_module: str,
    sdf_divider: str = "/",
) -> list[WireDelay]:
    """Resolve INTERCONNECT entries to wire delay annotations.

    Parameters
    ----------
    entries : list[BaseEntry]
        All entries from the top-level SDF cell (typically INTERCONNECT).
    design : YosysDesign
        Parsed Yosys design.
    top_module : str
        Name of the top-level module in the Yosys design.
    sdf_divider : str
        Hierarchy divider character from SDF header.

    Returns
    -------
    list[WireDelay]
        Wire delay annotations for net declarations.
    """
    if top_module not in design.modules:
        return []
    module = design.modules[top_module]
    bit_map = build_bit_to_net_map(module)

    wire_delays: list[WireDelay] = []
    for entry in entries:
        if entry.type != EntryType.INTERCONNECT:
            continue
        if not entry.to_pin or not entry.delay_paths:
            continue

        # Split to_pin on last divider to get (instance_path, pin_name)
        to_pin = entry.to_pin
        divider_pos = to_pin.rfind(sdf_divider)
        if divider_pos < 0:
            continue
        instance_path = to_pin[:divider_pos]
        pin_name = to_pin[divider_pos + 1 :]

        # Find instance in top module cells
        cell = module.cells.get(instance_path)
        if cell is None:
            continue

        # Get bit index from cell connections
        conn_bits = cell.connections.get(pin_name)
        if not conn_bits:
            continue

        # Look up net name from bit index
        for bit in conn_bits:
            if isinstance(bit, int) and bit in bit_map:
                net_name = bit_map[bit]
                rise_str, fall_str = _extract_rise_fall(entry.delay_paths)
                wd = WireDelay(
                    net_name=net_name,
                    rise_delay=rise_str,
                    fall_delay=fall_str,
                )
                wire_delays.append(wd)
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
    inserts ``#(rise, fall)`` after the ``wire`` keyword.

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
                    if wd.fall_delay is not None:
                        delay_str = f"#({wd.rise_delay}, {wd.fall_delay})"
                    else:
                        delay_str = f"#({wd.rise_delay})"
                    line = line.replace("wire ", f"wire {delay_str} ", 1)
                    break
        result.append(line)

    return "\n".join(result)


# ── Orchestrator ────────────────────────────────────────────────────


def annotate_verilog(
    sdf_path: Path,
    verilog_path: Path,
    output_path: Path | None = None,
    field_name: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
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
    field_name : str
        Delay field for worst-case selection (default: "slow").
    metric : str
        Metric for worst-case selection (default: "max").

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
        worst = select_worst_case_delays(entries, field_name, metric)
        specify_entries = entries_to_specify(worst)
        if specify_entries:
            module_blocks[mod_name] = render_specify_block(specify_entries)

    # 5. Resolve INTERCONNECT entries from top-level cell
    divider = sdf.header.divider or "/"
    top_module = sdf.header.design or ""
    top_entries: list[BaseEntry] = []
    for _cell_type, instances in sdf.cells.items():
        for _inst_name, inst_entries in instances.items():
            for entry in inst_entries.values():
                if entry.type == EntryType.INTERCONNECT:
                    top_entries.append(entry)

    wire_delays = resolve_interconnects(top_entries, design, top_module, divider)

    # 6. Insert into Verilog text
    verilog_text = verilog_path.read_text()
    verilog_text = insert_specify_blocks(verilog_text, module_blocks)
    verilog_text = insert_wire_delays(verilog_text, wire_delays)

    # 7. Write output
    if output_path is not None:
        output_path.write_text(verilog_text)

    return verilog_text
