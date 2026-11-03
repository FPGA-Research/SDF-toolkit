r"""Rewrite PORT interconnect delays as INTERCONNECT delays.

Some back-ends (Microchip Libero among them) express routing delay with the SDF
``(PORT <pin> ...)`` construct, which names only the load pin and leaves the
driver implicit. Tools that consume only ``(INTERCONNECT <source> <sink> ...)``,
which names both endpoints, cannot use those delays. This module rewrites each
PORT delay into the equivalent INTERCONNECT by recovering the driver from the
gate-level netlist.

The driver is read from connectivity, never guessed. Yosys parses the netlist
into bit-level nets (see :mod:`sdf_toolkit.io.annotate`). A cell pin is a driver
or a sink by the SDF's own IOPATH and PORT entries, else by the port direction
of the cell's module when the netlist declares one; top-level port directions
identify which ports drive the fabric. A pin with neither is unknown. A net with
no known driver resolves by elimination: when exactly one endpoint is unknown,
it is the driver. A net with a known driver and any unknown endpoint raises,
since that endpoint could be a second driver. Any pin that does not resolve to
exactly one driver raises ``DriverResolutionError``; nothing is dropped or
defaulted.

SDF names arrive escaped (``u_inv\[0\]``) and Yosys names bare
(``u_inv[0]``), so lookups into the netlist unescape the SDF name and driver
paths written back to SDF escape the Yosys name.

The transform is vendor- and device-neutral: it keys off SDF and netlist
structure only, never specific cell or tool names. It assumes a flat top-level
netlist (the PORT-bearing instances are direct children of the top module, as in
a back-annotated post-route netlist), scalar PORT pins, and single-driver nets.
Each unmet assumption raises rather than producing a wrong result.

A driver on one bit of a bus pin or bus port is written with its declared
index, as in ``u/DOUT[3]``.  Yosys gives connection bits from the least
significant, so the index comes from the range of the port declaration: the
top module's own port, or the port of the driving cell's module.  A bus pin of
a cell whose module the netlist does not define has no known range and raises.

The INTERCONNECT entries go on the top-level cell, written as the top module's
CELLTYPE with an empty INSTANCE, which IEEE 1497 reads as the design itself.
"""

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from sdf_toolkit.core.identifiers import sdf_escape, sdf_unescape
from sdf_toolkit.core.model import (
    Arrival,
    BidirectSkew,
    CellsDict,
    Departure,
    Device,
    Divider,
    Entry,
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
    Recovery,
    Recrem,
    Removal,
    SDFFile,
    Setup,
    SetupHold,
    Skew,
    SkewConstraint,
    Slack,
    Waveform,
    Width,
)
from sdf_toolkit.errors import SDFToolkitError
from sdf_toolkit.io.annotate import PortDirection, YosysDesign, YosysModule, YosysPort

# An empty INSTANCE names the top-level design in IEEE 1497.
TOP_INSTANCE = ""


class DriverResolutionError(SDFToolkitError, ValueError):
    """Raised when a PORT sink cannot be mapped to exactly one driver."""


class InterconnectConflictError(SDFToolkitError, ValueError):
    """Raised when a generated INTERCONNECT times an arc the top cell already has."""


@dataclass(frozen=True, slots=True)
class _CellPin:
    """A cell instance pin, or one bit of it, by its bare Yosys names.

    ``bit`` is the declared index of the bit on a bus pin, None on a scalar
    pin and on a bus pin of a cell type the netlist does not define.
    """

    instance: str
    pin: str
    cell_type: str
    bit: int | None = None


@dataclass(frozen=True, slots=True)
class _TopPort:
    """A top-level port of the netlist, or one bit of it.

    ``bit`` is the declared index of the bit on a bus port, None on a scalar.
    """

    port: str
    direction: PortDirection
    bit: int | None = None


_Endpoint = _CellPin | _TopPort


class _Role(StrEnum):
    """How an endpoint relates to its net."""

    DRIVER = "driver"
    SINK = "sink"
    UNKNOWN = "unknown"


_DIRECTION_ROLE = {
    PortDirection.INPUT: _Role.SINK,
    PortDirection.OUTPUT: _Role.DRIVER,
    PortDirection.INOUT: _Role.UNKNOWN,
}
# A top-level input drives the internal net; a top-level output loads it.
_TOP_PORT_ROLE = {
    PortDirection.INPUT: _Role.DRIVER,
    PortDirection.OUTPUT: _Role.SINK,
    PortDirection.INOUT: _Role.UNKNOWN,
}


@dataclass(frozen=True)
class _PinRoles:
    """Pin roles per cell type, from the SDF and the netlist's leaf modules.

    ``outputs`` and ``inputs`` hold bare pin names: IOPATH outputs are
    outputs; IOPATH inputs and PORT pins are inputs.
    """

    outputs: dict[str, set[str]]
    inputs: dict[str, set[str]]
    design: YosysDesign

    @classmethod
    def from_sdf(cls, sdf: SDFFile, design: YosysDesign) -> "_PinRoles":
        """Collect the pin roles the SDF's IOPATH and PORT entries imply."""
        outputs: dict[str, set[str]] = {}
        inputs: dict[str, set[str]] = {}
        for cell_type, instances in sdf.cells.items():
            for entries in instances.values():
                for entry in entries:
                    match entry:
                        case Iopath():
                            outputs.setdefault(cell_type, set()).add(
                                sdf_unescape(entry.output.port)
                            )
                            inputs.setdefault(cell_type, set()).add(
                                sdf_unescape(entry.input.port)
                            )
                        case Port():
                            inputs.setdefault(cell_type, set()).add(
                                sdf_unescape(entry.port.port)
                            )
                        case (
                            Interconnect()
                            | Device()
                            | NetDelay()
                            | PathPulse()
                            | PathPulsePercent()
                            | Setup()
                            | Hold()
                            | Recovery()
                            | Removal()
                            | SetupHold()
                            | Recrem()
                            | Skew()
                            | BidirectSkew()
                            | NoChange()
                            | Width()
                            | Period()
                            | PathConstraint()
                            | PeriodConstraint()
                            | SkewConstraint()
                            | Arrival()
                            | Departure()
                            | Slack()
                            | Waveform()
                            | Label()
                        ):
                            # Only IOPATH and PORT name a pin's direction.
                            pass
                        case _:
                            assert_never(entry)
        return cls(outputs=outputs, inputs=inputs, design=design)

    def classify(self, endpoint: _Endpoint) -> _Role:
        """Return the role of *endpoint* on its net."""
        match endpoint:
            case _TopPort(direction=direction):
                return _TOP_PORT_ROLE[direction]
            case _CellPin(pin=pin, cell_type=cell_type):
                if pin in self.outputs.get(cell_type, set()):
                    return _Role.DRIVER
                if pin in self.inputs.get(cell_type, set()):
                    return _Role.SINK
                module = self.design.modules.get(cell_type)
                if module is not None and pin in module.ports:
                    return _DIRECTION_ROLE[module.ports[pin].direction]
                return _Role.UNKNOWN


def _bit_index(port: YosysPort | None, *, position: int, width: int) -> int | None:
    """Return the declared index of bit *position* of a connection to *port*.

    Return None for a scalar, and for a bus whose *port* is unknown.
    """
    if width == 1 or port is None:
        return None
    return port.bit_index(position)


def _bit_endpoints(
    module: YosysModule, design: YosysDesign
) -> dict[int, list[_Endpoint]]:
    """Map each net bit of *module* to the cell pins and top ports on it."""
    endpoints: dict[int, list[_Endpoint]] = {}
    for inst, cell in module.cells.items():
        leaf = design.modules.get(cell.cell_type)
        for pin, bits in cell.connections.items():
            leaf_port = None if leaf is None else leaf.ports.get(pin)
            for position, bit in enumerate(bits):
                if isinstance(bit, int):
                    index = _bit_index(leaf_port, position=position, width=len(bits))
                    endpoints.setdefault(bit, []).append(
                        _CellPin(
                            instance=inst, pin=pin, cell_type=cell.cell_type, bit=index
                        )
                    )
    for port_name, port in module.ports.items():
        for position, bit in enumerate(port.bits):
            if isinstance(bit, int):
                index = _bit_index(port, position=position, width=len(port.bits))
                endpoints.setdefault(bit, []).append(
                    _TopPort(port=port_name, direction=port.direction, bit=index)
                )
    return endpoints


def _source_path(endpoint: _Endpoint, divider: Divider) -> str:
    """Write *endpoint* as an escaped SDF port path, as ``inst/pin[3]`` or ``port``."""
    match endpoint:
        case _TopPort(port=port, bit=bit):
            path = sdf_escape(port)
        case _CellPin(instance=instance, pin=pin, bit=bit):
            path = f"{sdf_escape(instance)}{divider}{sdf_escape(pin)}"
    return path if bit is None else f"{path}[{bit}]"


def _resolve_driver(
    *,
    others: list[_Endpoint],
    roles: _PinRoles,
    divider: Divider,
) -> _Endpoint:
    """Return the one driver among *others*, the other endpoints on a sink's net.

    Raises
    ------
    DriverResolutionError
        If the net has several known drivers, a known driver beside an
        endpoint of unknown role, or no driver that elimination can find.
    """
    by_role: dict[_Role, list[_Endpoint]] = {role: [] for role in _Role}
    for endpoint in others:
        by_role[roles.classify(endpoint)].append(endpoint)
    drivers, unknown = by_role[_Role.DRIVER], by_role[_Role.UNKNOWN]

    def _paths(endpoints: list[_Endpoint]) -> str:
        return ", ".join(_source_path(e, divider) for e in endpoints)

    if len(drivers) > 1:
        msg = f"several drivers on its net: {_paths(drivers)}"
        raise DriverResolutionError(msg)
    if drivers and unknown:
        msg = (
            f"driver {_paths(drivers)} shares its net with pins of unknown "
            f"direction that may also drive it: {_paths(unknown)}"
        )
        raise DriverResolutionError(msg)
    if drivers:
        return drivers[0]
    if len(unknown) == 1:
        return unknown[0]
    if not unknown:
        msg = "no driver on its net"
        raise DriverResolutionError(msg)
    msg = f"no known driver and several pins of unknown direction: {_paths(unknown)}"
    raise DriverResolutionError(msg)


def port_to_interconnect(
    sdf: SDFFile,
    design: YosysDesign,
    *,
    top_module: str | None = None,
) -> SDFFile:
    """Return a copy of *sdf* with PORT delays rewritten as INTERCONNECT delays.

    Each ``(PORT pin delay)`` on a cell instance is replaced by an
    ``(INTERCONNECT driver_path instance/pin delay)`` entry on the top-level
    cell ``(CELLTYPE "<top_module>") (INSTANCE)``, where the driver is
    recovered from *design*. The INTERCONNECT keeps the PORT's delay, delay
    mode and COND. The original PORT entries are removed.

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
    InterconnectConflictError
        If the top cell already has an entry on an arc a generated
        INTERCONNECT times.
    """
    if top_module is None:
        top_module = sdf.header.design
    if top_module is None:
        msg = "SDF header has no DESIGN; pass top_module explicitly"
        raise DriverResolutionError(msg)
    if top_module not in design.modules:
        msg = (
            f"top module {top_module!r} not found in netlist "
            f"(have {sorted(design.modules)})"
        )
        raise DriverResolutionError(msg)

    module = design.modules[top_module]
    divider = sdf.header.effective_divider
    roles = _PinRoles.from_sdf(sdf, design)
    endpoints = _bit_endpoints(module, design)

    cells: CellsDict = {}
    interconnects: list[Entry] = []
    unresolved: list[str] = []

    for cell_type, instances in sdf.cells.items():
        kept_instances = cells.setdefault(cell_type, {})
        for inst, entries in instances.items():
            kept_instances[inst] = [e for e in entries if not isinstance(e, Port)]
            cell = module.cells.get(sdf_unescape(inst))
            for entry in entries:
                if not isinstance(entry, Port):
                    continue
                sink_path = f"{inst}{divider}{entry.port.port}"
                if cell is None:
                    unresolved.append(f"{sink_path} (instance not in netlist)")
                    continue
                pin = sdf_unescape(entry.port.port)
                if pin not in cell.connections:
                    unresolved.append(f"{sink_path} (pin not in netlist)")
                    continue
                bits = cell.connections[pin]
                if len(bits) != 1:
                    unresolved.append(
                        f"{sink_path} (expected a scalar pin, found {len(bits)} bits)"
                    )
                    continue
                bit = bits[0]
                if not isinstance(bit, int):
                    unresolved.append(f"{sink_path} (tied to constant {bit!r})")
                    continue
                sink = _CellPin(instance=cell.name, pin=pin, cell_type=cell.cell_type)
                try:
                    source = _resolve_driver(
                        others=[e for e in endpoints[bit] if e != sink],
                        roles=roles,
                        divider=divider,
                    )
                except DriverResolutionError as exc:
                    unresolved.append(f"{sink_path} ({exc})")
                    continue
                if (
                    isinstance(source, _CellPin)
                    and source.bit is None
                    and len(module.cells[source.instance].connections[source.pin]) > 1
                ):
                    unresolved.append(
                        f"{sink_path} (driver {_source_path(source, divider)} is a "
                        f"bus pin of {source.cell_type}, which the netlist does not "
                        "define, so the bit index is unknown; add the module "
                        "declaration to the netlist)"
                    )
                    continue
                interconnects.append(
                    Interconnect(
                        source=PortSpec(_source_path(source, divider)),
                        load=PortSpec(sink_path),
                        delay=entry.delay,
                        mode=entry.mode,
                        cond=entry.cond,
                    )
                )

    if unresolved:
        joined = "\n  ".join(unresolved)
        msg = f"could not resolve a unique driver for these PORT sinks:\n  {joined}"
        raise DriverResolutionError(msg)

    if interconnects:
        top_entries = cells.setdefault(top_module, {}).setdefault(TOP_INSTANCE, [])
        present = {e.key for e in top_entries}
        clashes = [str(e.key) for e in interconnects if e.key in present]
        if clashes:
            joined = "\n  ".join(clashes)
            msg = f"the top cell {top_module!r} already times these arcs:\n  {joined}"
            raise InterconnectConflictError(msg)
        top_entries.extend(interconnects)

    return SDFFile(header=sdf.header, cells=cells)
