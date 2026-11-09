"""Back-annotation of a flat gate-level netlist through transport buffers.

{func}`sdf_toolkit.io.annotate.annotate_verilog` gives each cell type one
specify block and each net one delay.  A routed FPGA netlist needs neither
reduction: every instance and every sink of a net has its own delay.  Its
delays must also be transport, because a Verilog gate, net or path delay is
inertial and swallows any pulse narrower than itself, while a routed wire
carries that pulse.

So each sink pin moves onto a fresh net driven by a buffer whose delay is the
driver's slowest arc plus the routing to that sink.  Yosys supplies the
connectivity and writes the netlist.  The stages are public so that a caller
can edit the Yosys JSON between them, and {func}`annotate_transport` runs them
in order.
"""

import json
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sdf_toolkit.core.delays import Interconnect, Iopath, Port
from sdf_toolkit.core.enums import Corner, EdgeSelect
from sdf_toolkit.core.sdffile import SDFFile
from sdf_toolkit.core.values import MissingDelayError
from sdf_toolkit.io.annotate import (
    AnnotationError,
    YosysCellJson,
    YosysError,
    YosysJson,
    YosysModuleJson,
    require_absolute,
    yosys_running,
)
from sdf_toolkit.parser.standard import SDFStandard

# (instance, pin), with the instance spelled as Yosys spells it.
type Pin = tuple[str, str]

TRANSPORT_BUFFER = "sdf_transport_buffer"

_DELAY_PARAMETER = "DLY_FS"
_PARAMETER_BITS = 64
_FS_PER_NS = 10**6


@dataclass(frozen=True, slots=True)
class RoutedDelays:
    """The delays of one SDF in nanoseconds.

    ``sinks`` holds the PORT delay of each input pin and ``outputs`` the
    slowest IOPATH into each output pin.  The bits of a bus pin share their
    slowest value, because a pin is keyed by name.
    """

    sinks: dict[Pin, Decimal]
    outputs: dict[Pin, Decimal]


def _keep_slowest(delays: dict[Pin, Decimal], pin: Pin, delay: Decimal) -> None:
    delays[pin] = max(delays.get(pin, delay), delay)


def routed_delays(
    sdf: SDFFile,
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
) -> RoutedDelays:
    """Read the PORT and IOPATH delays of *sdf* at *corner* with *edge*.

    An entry with no value at that corner and edge is skipped, since the
    format allows an empty rvalue.

    Raises
    ------
    AnnotationError
        If the SDF has no TIMESCALE, holds an INCREMENT delay, or holds an
        INTERCONNECT, whose source pin a per-sink delay cannot honour.
    """
    timescale = sdf.header.timescale
    if timescale is None:
        msg = "The SDF has no TIMESCALE, so its delays have no unit"
        raise AnnotationError(msg)
    ns_per_unit = Decimal(timescale.femtoseconds) / _FS_PER_NS

    sinks: dict[Pin, Decimal] = {}
    outputs: dict[Pin, Decimal] = {}
    for instances in sdf.cells.values():
        for instance, entries in instances.items():
            name = instance.replace("\\", "")
            for entry in entries:
                if isinstance(entry, Interconnect):
                    msg = (
                        f"{name}: INTERCONNECT {entry.source.port} to "
                        f"{entry.load.port} has no transport form; write the "
                        "routing as PORT delays of its loads"
                    )
                    raise AnnotationError(msg)
                if not isinstance(entry, Port | Iopath):
                    continue
                require_absolute(entry, context=name)
                try:
                    delay = entry.delay.scalar(corner=corner, edge=edge) * ns_per_unit
                except MissingDelayError:
                    continue
                if isinstance(entry, Port):
                    _keep_slowest(sinks, (name, entry.port.port), delay)
                else:
                    _keep_slowest(outputs, (name, entry.output.port), delay)
    return RoutedDelays(sinks=sinks, outputs=outputs)


def _highest_bit(module: YosysModuleJson) -> int:
    signals = [
        *(
            bits
            for cell in module["cells"].values()
            for bits in cell["connections"].values()
        ),
        *(net["bits"] for net in module["netnames"].values()),
    ]
    return max(
        (bit for bits in signals for bit in bits if isinstance(bit, int)), default=1
    )


def _delay_parameter(delay_ns: Decimal, *, name: str) -> str:
    femtoseconds = round(delay_ns * _FS_PER_NS)
    if not 0 <= femtoseconds < 1 << _PARAMETER_BITS:
        msg = f"{name}: delay {delay_ns} ns has no {_PARAMETER_BITS}-bit fs form"
        raise AnnotationError(msg)
    return format(femtoseconds, f"0{_PARAMETER_BITS}b")


def insert_transport_buffers(
    module: YosysModuleJson,
    delays: RoutedDelays,
    *,
    buffer: str = TRANSPORT_BUFFER,
) -> dict[str, Decimal]:
    """Rewire every sink pin of *module* through a *buffer*, in place.

    A pin the SDF does not name has no delay of its own, so its buffer carries
    the driver's delay alone.  Returns each buffer's delay in nanoseconds by
    instance name.

    Raises
    ------
    AnnotationError
        If a delay is negative or does not fit the buffer's parameter.
    """
    drivers: dict[int, Pin] = {}
    sinks: dict[int, list[Pin]] = {}
    for name, cell in module["cells"].items():
        directions = cell.get("port_directions", {})
        for port, bits in cell["connections"].items():
            for bit in bits:
                if not isinstance(bit, int):
                    continue
                if directions.get(port) == "output":
                    drivers[bit] = (name, port)
                else:
                    sinks.setdefault(bit, []).append((name, port))

    next_bit = _highest_bit(module) + 1
    inserted: dict[str, Decimal] = {}
    for bit, loads in sinks.items():
        driver = drivers.get(bit)
        driver_ns = Decimal(0)
        if driver is not None:
            driver_ns = delays.outputs.get(
                (driver[0].replace("\\", ""), driver[1]), Decimal(0)
            )
        for load, port in loads:
            delay = driver_ns + delays.sinks.get(
                (load.replace("\\", ""), port), Decimal(0)
            )
            connection = module["cells"][load]["connections"]
            connection[port] = [next_bit if b == bit else b for b in connection[port]]
            name = f"{buffer}_b{len(inserted)}"
            cell: YosysCellJson = {
                "hide_name": 0,
                "type": buffer,
                "parameters": {_DELAY_PARAMETER: _delay_parameter(delay, name=name)},
                "attributes": {},
                "port_directions": {"A": "input", "Y": "output"},
                "connections": {"A": [bit], "Y": [next_bit]},
            }
            module["cells"][name] = cell
            inserted[name] = delay
            next_bit += 1
    return inserted


def transport_buffer_verilog(buffer: str = TRANSPORT_BUFFER) -> str:
    """Return the Verilog definition of *buffer*.

    The non-blocking intra-assignment delay schedules every input edge on its
    own, so a pulse narrower than the delay survives.  The explicit timescale
    keeps the delay in nanoseconds whatever order the simulator compiles in,
    and its 1 ps precision rounds the femtosecond parameter when simulated.
    """
    return f"""
`timescale 1ns/1ps
module {buffer} (Y, A);
  parameter {_DELAY_PARAMETER} = 0;
  output reg Y;
  input A;
  // `always @(A)` sees changes only, so a net that never toggles would stay X.
  initial Y = A;
  always @(A) Y <= #({_DELAY_PARAMETER} * 1.0e-6) A;
endmodule
"""


def write_transport_verilog(
    design: YosysJson, *, buffer: str = TRANSPORT_BUFFER
) -> str:
    """Write *design* as Verilog, followed by the definition of *buffer*.

    Raises
    ------
    YosysError
        If Yosys is missing or fails.
    """
    blackbox: YosysModuleJson = {
        "attributes": {"blackbox": "1"},
        "ports": {
            "A": {"direction": "input", "bits": [2]},
            "Y": {"direction": "output", "bits": [3]},
        },
        "cells": {},
        "netnames": {},
    }
    with_buffer = {**design, "modules": {**design["modules"], buffer: blackbox}}
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "design.json"
        written = Path(directory) / "design.v"
        source.write_text(json.dumps(with_buffer))
        script = f"read_json {source}; write_verilog -noattr {written}"
        try:
            result = subprocess.run(  # noqa: S603
                ["yosys", "-q", "-p", script],  # noqa: S607
                capture_output=True,
                text=True,
                check=False,
            )
        except FileNotFoundError:
            msg = "Yosys is required for Verilog annotation but was not found"
            raise YosysError(msg) from None
        if result.returncode != 0:
            msg = f"Yosys failed (exit {result.returncode}):\n{result.stderr}"
            raise YosysError(msg)
        return written.read_text() + transport_buffer_verilog(buffer)


def annotate_transport(
    sdf_path: Path,
    verilog_path: Path,
    *,
    top: str,
    library: Sequence[Path] = (),
    output_path: Path | None = None,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
    standard: SDFStandard | None = None,
    loose_cond: bool = False,
) -> str:
    """Annotate the flat module *top* with the delays of an SDF file.

    Each *library* file defines cells as port-only views, which give Yosys the
    pin directions.  The annotated text is written to *output_path* when given,
    and returned.  *standard* and *loose_cond* set the rules the SDF is parsed
    under, as in {func}`sdf_toolkit.parser.parse_sdf`.

    Raises
    ------
    AnnotationError
        If the SDF has no transport form; see {func}`routed_delays`.
    YosysError
        If Yosys is missing or fails.
    """
    from sdf_toolkit.parser.parser import deferred_cyclic_gc, parse_sdf

    # The netlist and the SDF are independent until the delays are placed.
    with (
        yosys_running(verilog_path, library=library, top=top) as yosys_json,
        deferred_cyclic_gc(),
    ):
        sdf = parse_sdf(sdf_path.read_text(), standard=standard, loose_cond=loose_cond)
        delays = routed_delays(sdf, corner=corner, edge=edge)
        design = yosys_json()
        insert_transport_buffers(design["modules"][top], delays)
        verilog_text = write_transport_verilog(design)

    if output_path is not None:
        output_path.write_text(verilog_text)
    return verilog_text
