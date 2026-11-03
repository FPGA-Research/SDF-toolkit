#!/usr/bin/env python3
#
# Copyright 2020-2022 F4PGA Authors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# SPDX-License-Identifier: Apache-2.0
"""SDF file writer.

Each block is a list of lines built in Python, so the type checker sees
every entry field and every entry class the ``match`` arms must cover.
Cell types and instances are written in sorted order; entries keep the
order of their cell list within each block.  The header writes only the
fields the file sets, so a file without ``TIMESCALE`` or ``DIVIDER``
round-trips without gaining one, and an empty ``(PROCESS)`` string is
written as ``(PROCESS "")`` rather than dropped.
"""

from typing import assert_never

from sdf_toolkit.core.model import (
    Cond,
    CondElse,
    DelayEntry,
    DelayMode,
    DelayValues,
    Device,
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
    TimingCheck,
    TimingPortSpec,
    Values,
    Width,
)

_COND_INDENT = " " * 20
_BLOCK_INDENT = " " * 8
_ENTRY_INDENT = " " * 16
_DELAY_MODE_INDENT = " " * 12


def _number(value: float | None) -> str:
    return "" if value is None else str(value)


def format_values(values: Values) -> str:
    """Write a triple as ``(min:typ:max)``, or ``()`` when every corner is omitted."""
    if values.is_empty():
        return "()"
    return f"({_number(values.min)}:{_number(values.typ)}:{_number(values.max)})"


def format_delay(delay: DelayValues) -> str:
    """Write the rvalues of a delay in file order."""
    return "".join(format_values(v) for v in delay.rvalues)


def format_port(spec: PortSpec) -> str:
    """Write a port with its edge, as ``(posedge CK)`` or ``CK``."""
    return f"({spec.edge} {spec.port})" if spec.edge is not None else spec.port


def _cond_head(cond: Cond) -> str:
    name = "" if cond.name is None else f'"{cond.name}" '
    return f"(COND {name}"


def format_timing_port(spec: TimingPortSpec) -> str:
    """Write a timing check port, wrapped in its ``COND`` when it has one."""
    port = f"({spec.edge} {spec.port})" if spec.edge is not None else spec.port
    if spec.cond is None:
        return port
    return f"{_cond_head(spec.cond)}{spec.cond.equation} {port})"


def _delay_body(entry: DelayEntry) -> str:
    delay = format_delay(entry.delay)
    match entry:
        case Iopath():
            return (
                f"(IOPATH {format_port(entry.input)} "
                f"{format_port(entry.output)} {delay})"
            )
        case Port():
            return f"(PORT {format_port(entry.port)} {delay})"
        case Interconnect():
            return (
                f"(INTERCONNECT {format_port(entry.source)} "
                f"{format_port(entry.load)} {delay})"
            )
        case Device(output=None):
            return f"(DEVICE {delay})"
        case Device(output=output):
            return f"(DEVICE {format_port(output)} {delay})"
        case _:
            assert_never(entry)


def format_delay_entry(entry: DelayEntry) -> str:
    """Write a delay entry, wrapped in its ``COND`` or ``CONDELSE``."""
    body = _delay_body(entry)
    match entry.cond:
        case None:
            return body
        case Cond():
            head = f"{_cond_head(entry.cond)}({entry.cond.equation})"
        case CondElse():
            head = "(CONDELSE"
        case _:
            assert_never(entry.cond)
    return f"{head}\n{_COND_INDENT}{body}\n{_COND_INDENT})"


def format_timing_check(entry: TimingCheck) -> str:
    """Write a timing check with its ports in SDF order, data port first."""
    keyword = entry.type.upper()
    match entry:
        case Width() | Period():
            return (
                f"({keyword} {format_timing_port(entry.port)} "
                f"{format_values(entry.limit)})"
            )
        case SetupHold():
            return (
                f"({keyword} {format_timing_port(entry.data)} "
                f"{format_timing_port(entry.reference)} "
                f"{format_values(entry.setup)} {format_values(entry.hold)})"
            )
        case Setup() | Hold() | Recovery() | Removal():
            return (
                f"({keyword} {format_timing_port(entry.data)} "
                f"{format_timing_port(entry.reference)} {format_values(entry.limit)})"
            )
        case _:
            assert_never(entry)


def format_path_constraint(entry: PathConstraint) -> str:
    """Write a path constraint with its ports in file order."""
    ports = " ".join(entry.ports)
    return (
        f"(PATHCONSTRAINT {ports} {format_values(entry.rise)} "
        f"{format_values(entry.fall)})"
    )


def _block(keyword: str, lines: list[str], *, indent: str, body: str) -> list[str]:
    """Wrap *lines* in ``(KEYWORD`` and ``)``; an empty block writes nothing."""
    if not lines:
        return []
    return [f"{indent}({keyword}", *(f"{body}{line}" for line in lines), f"{indent})"]


def _instance_lines(entries: list[Entry]) -> list[str]:
    """Write the ``DELAY``, ``TIMINGCHECK`` and ``TIMINGENV`` blocks of one instance."""
    absolute: list[str] = []
    increment: list[str] = []
    checks: list[str] = []
    constraints: list[str] = []
    for entry in entries:
        match entry:
            case Iopath() | Port() | Interconnect() | Device():
                match entry.mode:
                    case DelayMode.ABSOLUTE:
                        absolute.append(format_delay_entry(entry))
                    case DelayMode.INCREMENT:
                        increment.append(format_delay_entry(entry))
                    case _:
                        assert_never(entry.mode)
            case (
                Setup()
                | Hold()
                | Recovery()
                | Removal()
                | SetupHold()
                | Width()
                | Period()
            ):
                checks.append(format_timing_check(entry))
            case PathConstraint():
                constraints.append(format_path_constraint(entry))
            case _:
                assert_never(entry)
    modes = [
        *_block("ABSOLUTE", absolute, indent=_DELAY_MODE_INDENT, body=_ENTRY_INDENT),
        *_block("INCREMENT", increment, indent=_DELAY_MODE_INDENT, body=_ENTRY_INDENT),
    ]
    return [
        *_block("DELAY", modes, indent=_BLOCK_INDENT, body=""),
        *_block("TIMINGCHECK", checks, indent=_BLOCK_INDENT, body=_ENTRY_INDENT),
        *_block("TIMINGENV", constraints, indent=_BLOCK_INDENT, body=_ENTRY_INDENT),
    ]


def _header_lines(header: SDFHeader) -> list[str]:
    """Write the header fields the file sets, in IEEE 1497 order."""
    quoted = [
        ("SDFVERSION", header.sdfversion),
        ("DESIGN", header.design),
        ("DATE", header.date),
        ("VENDOR", header.vendor),
        ("PROGRAM", header.program),
        ("VERSION", header.version),
    ]
    lines = [f'    ({key} "{value}")' for key, value in quoted if value is not None]
    if header.divider is not None:
        lines.append(f"    (DIVIDER {header.divider})")
    if header.voltage is not None:
        lines.append(f"    (VOLTAGE {header.voltage})")
    if header.process is not None:
        lines.append(f'    (PROCESS "{header.process}")')
    if header.temperature is not None:
        lines.append(f"    (TEMPERATURE {header.temperature})")
    if header.timescale is not None:
        lines.append(f"    (TIMESCALE {header.timescale})")
    return lines


def emit_sdf(timings: SDFFile, *, uppercase_celltype: bool = False) -> str:
    """Write a complete SDF file, cell types and instances in sorted order."""
    lines = ["(DELAYFILE", *_header_lines(timings.header), ""]
    for cell_name in sorted(timings.cells):
        celltype = cell_name.upper() if uppercase_celltype else cell_name
        instances = timings.cells[cell_name]
        for instance_name in sorted(instances):
            lines += [
                "    (CELL",
                f'        (CELLTYPE "{celltype}")',
                f"        (INSTANCE {instance_name})",
                *_instance_lines(instances[instance_name]),
                "    )",
            ]
    lines.append(")")
    return "\n".join(lines) + "\n"
