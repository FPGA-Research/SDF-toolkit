"""Rewrite PORT interconnect delays as INTERCONNECT delays.

Some back-ends (Microchip Libero among them) express routing delay with the SDF
``(PORT <pin> ...)`` construct, which names only the load pin and leaves the
driver implicit. Tools that consume only ``(INTERCONNECT <source> <sink> ...)``,
which names both endpoints, cannot use those delays. This module rewrites each
PORT delay into the equivalent INTERCONNECT by recovering the driver from the
gate-level netlist.

The driver is read from connectivity, never guessed. Yosys parses the netlist
into bit-level nets (see :mod:`sdf_toolkit.io.annotate`); the SDF's own IOPATH
entries identify which cell pins are outputs; top-level port directions identify
which ports drive the fabric. A cell the SDF does not time, whose pin roles are
therefore unknown, is resolved by elimination: on a single-driver net whose every
other endpoint is a known sink, the one remaining endpoint is the driver. Any pin
that does not resolve to exactly one driver raises ``DriverResolutionError``;
nothing is dropped or defaulted.

The transform is vendor- and device-neutral: it keys off SDF and netlist
structure only, never specific cell or tool names. It assumes a flat top-level
netlist (the PORT-bearing instances are direct children of the top module, as in
a back-annotated post-route netlist), scalar PORT pins, and single-driver nets.
Each unmet assumption raises rather than producing a wrong result.
"""

import copy
from typing import NamedTuple

from sdf_toolkit.core.builder import make_interconnect
from sdf_toolkit.core.model import BaseEntry, EntryType, SDFFile
from sdf_toolkit.io.annotate import PortDirection, YosysDesign, YosysModule


class DriverResolutionError(ValueError):
    """Raised when a PORT sink cannot be mapped to exactly one driver."""


class _Endpoint(NamedTuple):
    """One pin attached to a net: a cell instance pin, or a top-level port."""

    instance: str | None  # None marks a top-level port
    pin: str
    cell_type: str | None  # cell type for instance pins; None for top ports
    top_direction: PortDirection | None  # set only for top-level ports


class _Role:
    """How an endpoint relates to its net."""

    DRIVER = "driver"
    SINK = "sink"
    UNKNOWN = "unknown"


def _pin_roles(sdf: SDFFile) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """Return ``(outputs, inputs)`` pin-name sets per cell type from the SDF.

    IOPATH ``to_pin`` names are outputs; IOPATH ``from_pin`` names and PORT pins
    are inputs.

    Parameters
    ----------
    sdf : SDFFile
        The SDF whose cells carry the IOPATH and PORT entries.

    Returns
    -------
    tuple[dict[str, set[str]], dict[str, set[str]]]
        Output pin names and input pin names, keyed by cell type.
    """
    outputs: dict[str, set[str]] = {}
    inputs: dict[str, set[str]] = {}
    for cell_type, instances in sdf.cells.items():
        for entries in instances.values():
            for entry in entries.values():
                if entry.type == EntryType.IOPATH:
                    if entry.to_pin:
                        outputs.setdefault(cell_type, set()).add(entry.to_pin)
                    if entry.from_pin:
                        inputs.setdefault(cell_type, set()).add(entry.from_pin)
                elif entry.type == EntryType.PORT and entry.from_pin:
                    inputs.setdefault(cell_type, set()).add(entry.from_pin)
    return outputs, inputs


def _bit_endpoints(module: YosysModule) -> dict[int, list[_Endpoint]]:
    """Map each net bit to the cell pins and top ports attached to it.

    Parameters
    ----------
    module : YosysModule
        The top-level module whose connectivity defines the nets.

    Returns
    -------
    dict[int, list[_Endpoint]]
        Endpoints keyed by net bit index.
    """
    endpoints: dict[int, list[_Endpoint]] = {}
    for inst, cell in module.cells.items():
        for pin, bits in cell.connections.items():
            for bit in bits:
                if isinstance(bit, int):
                    endpoints.setdefault(bit, []).append(
                        _Endpoint(inst, pin, cell.cell_type, None)
                    )
    for port_name, port in module.ports.items():
        for bit in port.bits:
            if isinstance(bit, int):
                endpoints.setdefault(bit, []).append(
                    _Endpoint(None, port_name, None, port.direction)
                )
    return endpoints


def _classify(
    endpoint: _Endpoint,
    outputs: dict[str, set[str]],
    inputs: dict[str, set[str]],
) -> str:
    """Classify an endpoint as a driver, a sink, or unknown."""
    if endpoint.instance is None:
        # A top-level input port drives the internal net; an output port loads it.
        if endpoint.top_direction == PortDirection.INPUT:
            return _Role.DRIVER
        return _Role.SINK
    cell_type = endpoint.cell_type or ""
    if endpoint.pin in outputs.get(cell_type, ()):
        return _Role.DRIVER
    if endpoint.pin in inputs.get(cell_type, ()):
        return _Role.SINK
    return _Role.UNKNOWN


def _source_path(endpoint: _Endpoint, divider: str) -> str:
    """Format an endpoint as an SDF port path (``inst/pin`` or top ``port``)."""
    if endpoint.instance is None:
        return endpoint.pin
    return f"{endpoint.instance}{divider}{endpoint.pin}"


def _resolve_driver(
    sink: tuple[str, str],
    bit: int,
    endpoints: dict[int, list[_Endpoint]],
    outputs: dict[str, set[str]],
    inputs: dict[str, set[str]],
    divider: str,
) -> str | None:
    """Return the unique driver path for the net at *bit*, or None if ambiguous.

    A net is driven by exactly one source. The driver is the unique endpoint that
    classifies as a driver; if none does (the driving cell is untimed), it is the
    unique endpoint that is not a known sink. Zero or several candidates leave the
    net unresolved.

    Parameters
    ----------
    sink : tuple[str, str]
        The ``(instance, pin)`` of the PORT load, excluded from the candidates.
    bit : int
        The net bit shared by the driver and the sink.
    endpoints : dict[int, list[_Endpoint]]
        All endpoints keyed by net bit.
    outputs, inputs : dict[str, set[str]]
        Output and input pin names per cell type.
    divider : str
        Hierarchy divider for formatting the driver path.

    Returns
    -------
    str | None
        The driver port path, or None when it is not unique.
    """
    others = [
        endpoint
        for endpoint in endpoints.get(bit, [])
        if (endpoint.instance, endpoint.pin) != sink
    ]
    candidates = [
        endpoint
        for endpoint in others
        if _classify(endpoint, outputs, inputs) == _Role.DRIVER
    ]
    if not candidates:
        candidates = [
            endpoint
            for endpoint in others
            if _classify(endpoint, outputs, inputs) == _Role.UNKNOWN
        ]
    if len(candidates) == 1:
        return _source_path(candidates[0], divider)
    return None


def port_to_interconnect(
    sdf: SDFFile,
    design: YosysDesign,
    top_module: str | None = None,
) -> SDFFile:
    """Return a copy of *sdf* with PORT delays rewritten as INTERCONNECT delays.

    Each ``(PORT pin delay)`` on a cell instance is replaced by an
    ``(INTERCONNECT driver_path instance/pin delay)`` entry on the top-level cell,
    where the driver is recovered from *design*. The original PORT entries are
    removed.

    Parameters
    ----------
    sdf : SDFFile
        The SDF carrying PORT delays (typically from Libero ``EXPORTSDF``).
    design : YosysDesign
        The gate-level netlist parsed from Verilog via Yosys.
    top_module : str | None
        Top module name. Defaults to the SDF header ``DESIGN``.

    Returns
    -------
    SDFFile
        A new SDF with INTERCONNECT entries in place of PORT entries.

    Raises
    ------
    DriverResolutionError
        If the top module is missing, or any PORT sink does not resolve to
        exactly one driver. The message lists every unresolved sink.
    """
    top_module = top_module or sdf.header.design
    if not top_module:
        msg = "SDF header has no DESIGN; pass top_module explicitly"
        raise DriverResolutionError(msg)
    if top_module not in design.modules:
        msg = (
            f"top module {top_module!r} not found in netlist "
            f"(have {sorted(design.modules)})"
        )
        raise DriverResolutionError(msg)

    module = design.modules[top_module]
    divider = sdf.header.divider or "/"
    outputs, inputs = _pin_roles(sdf)
    endpoints = _bit_endpoints(module)

    result = copy.deepcopy(sdf)
    interconnects: dict[str, BaseEntry] = {}
    unresolved: list[str] = []

    for instances in result.cells.values():
        for inst, entries in instances.items():
            cell = module.cells.get(inst)
            port_keys = [
                key for key, entry in entries.items() if entry.type == EntryType.PORT
            ]
            for key in port_keys:
                entry = entries.pop(key)
                pin = entry.from_pin or ""
                sink_path = f"{inst}{divider}{pin}"
                if cell is None:
                    unresolved.append(f"{sink_path} (instance not in netlist)")
                    continue
                bits = [b for b in cell.connections.get(pin, []) if isinstance(b, int)]
                if len(bits) != 1:
                    unresolved.append(
                        f"{sink_path} (expected a scalar pin, found {len(bits)} bits)"
                    )
                    continue
                source = _resolve_driver(
                    (inst, pin), bits[0], endpoints, outputs, inputs, divider
                )
                if source is None:
                    unresolved.append(f"{sink_path} (no unique driver on its net)")
                    continue
                # Reuse the builder factory for the canonical entry + naming;
                # carry over the PORT's absolute/incremental context so the entry
                # emits in the same delay block.
                interconnect = make_interconnect(source, sink_path, entry.delay_paths)
                interconnect.is_absolute = entry.is_absolute
                interconnect.is_incremental = entry.is_incremental
                interconnects[interconnect.name] = interconnect

    if unresolved:
        joined = "\n  ".join(unresolved)
        msg = f"could not resolve a unique driver for these PORT sinks:\n  {joined}"
        raise DriverResolutionError(msg)

    if interconnects:
        top_cell = result.cells.setdefault(top_module, {}).setdefault(top_module, {})
        top_cell.update(interconnects)

    return result
