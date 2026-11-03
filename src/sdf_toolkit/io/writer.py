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
    Arrival,
    Cond,
    CondElse,
    DelayEntry,
    DelayMode,
    DelayValues,
    Departure,
    Device,
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
    PulseEntry,
    PulseLimits,
    Recovery,
    Recrem,
    Removal,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    Skew,
    SkewConstraint,
    Slack,
    TimingCheck,
    TimingEnvEntry,
    TimingPortSpec,
    Values,
    Waveform,
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


def _format_delval(value: Values, limits: PulseLimits | None) -> str:
    if limits is None:
        return format_values(value)
    error = "" if limits.error is None else f" {format_values(limits.error)}"
    return f"({format_values(value)} {format_values(limits.reject)}{error})"


def format_delay(delay: DelayValues) -> str:
    """Write the delvals of a delay in file order, pulse limits included."""
    limits = delay.limits or (None,) * len(delay.rvalues)
    return "".join(
        _format_delval(value, limit)
        for value, limit in zip(delay.rvalues, limits, strict=True)
    )


def format_port(spec: PortSpec) -> str:
    """Write a port with its edge, as ``(posedge CK)`` or ``CK``."""
    return f"({spec.edge} {spec.port})" if spec.edge is not None else spec.port


def _cond_name(cond: Cond) -> str:
    return "" if cond.name is None else f'"{cond.name}" '


def _cond_head(cond: Cond) -> str:
    return f"(COND {_cond_name(cond)}"


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
            retain = "".join(
                f"(RETAIN {''.join(format_values(v) for v in r.rvalues)}) "
                for r in entry.retain
            )
            return (
                f"(IOPATH {format_port(entry.input)} "
                f"{format_port(entry.output)} {retain}{delay})"
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
        case NetDelay():
            return f"(NETDELAY {entry.net} {delay})"
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


def format_pulse_entry(entry: PulseEntry) -> str:
    """Write a PATHPULSE or PATHPULSEPERCENT deltype."""
    path = "" if entry.path is None else f"{entry.path[0]} {entry.path[1]} "
    error = "" if entry.error is None else f" {format_values(entry.error)}"
    return f"({entry.type.upper()} {path}{format_values(entry.reject)}{error})"


def _check_conds(entry: SetupHold | Recrem) -> str:
    return "".join(
        f" ({keyword} {_cond_name(cond)}{cond.equation})"
        for keyword, cond in (("SCOND", entry.scond), ("CCOND", entry.ccond))
        if cond is not None
    )


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
                f"{format_values(entry.setup)} {format_values(entry.hold)}"
                f"{_check_conds(entry)})"
            )
        case Recrem():
            return (
                f"({keyword} {format_timing_port(entry.data)} "
                f"{format_timing_port(entry.reference)} "
                f"{format_values(entry.recovery)} {format_values(entry.removal)}"
                f"{_check_conds(entry)})"
            )
        case Setup() | Hold() | Recovery() | Removal():
            return (
                f"({keyword} {format_timing_port(entry.data)} "
                f"{format_timing_port(entry.reference)} {format_values(entry.limit)})"
            )
        case Skew():
            return (
                f"({keyword} {format_timing_port(entry.first)} "
                f"{format_timing_port(entry.second)} {format_values(entry.limit)})"
            )
        case NoChange():
            return (
                f"({keyword} {format_timing_port(entry.first)} "
                f"{format_timing_port(entry.second)} "
                f"{format_values(entry.start)} {format_values(entry.end)})"
            )
        case _:
            assert_never(entry)


def _format_four(values: tuple[Values, Values, Values, Values]) -> str:
    return " ".join(format_values(v) for v in values)


def format_timing_env(entry: TimingEnvEntry) -> str:
    """Write a ``TIMINGENV`` entry with its ports in file order."""
    keyword = entry.type.upper()
    match entry:
        case PathConstraint():
            name = "" if entry.name is None else f'(NAME "{entry.name}") '
            return (
                f"({keyword} {name}{' '.join(entry.ports)} "
                f"{format_values(entry.rise)} {format_values(entry.fall)})"
            )
        case PeriodConstraint():
            instances = " ".join(
                f"(INSTANCE {name})" if name else "(INSTANCE)"
                for name in entry.exception
            )
            exception = f" (EXCEPTION {instances})" if entry.exception else ""
            return f"({keyword} {entry.port} {format_values(entry.limit)}{exception})"
        case SkewConstraint():
            return f"({keyword} {format_port(entry.port)} {format_values(entry.limit)})"
        case Arrival() | Departure():
            reference = (
                "" if entry.reference is None else f"{format_port(entry.reference)} "
            )
            return f"({keyword} {reference}{entry.port} {_format_four(entry.values)})"
        case Slack():
            period = "" if entry.period is None else f" {entry.period}"
            return f"({keyword} {entry.port} {_format_four(entry.values)}{period})"
        case Waveform():
            edges = " ".join(
                f"({e.edge} {' '.join(str(o) for o in e.offsets)})" for e in entry.edges
            )
            return f"({keyword} {entry.port} {entry.period} {edges})"
        case _:
            assert_never(entry)


def _block(keyword: str, lines: list[str], *, indent: str, body: str) -> list[str]:
    """Wrap *lines* in ``(KEYWORD`` and ``)``; an empty block writes nothing."""
    if not lines:
        return []
    return [f"{indent}({keyword}", *(f"{body}{line}" for line in lines), f"{indent})"]


def _instance_lines(entries: list[Entry]) -> list[str]:
    """Write the ``DELAY``, ``TIMINGCHECK``, ``TIMINGENV`` and ``LABEL`` blocks.

    Pulse deltypes follow the ``ABSOLUTE`` and ``INCREMENT`` blocks inside
    ``DELAY``.
    """
    absolute: list[str] = []
    increment: list[str] = []
    pulses: list[str] = []
    checks: list[str] = []
    constraints: list[str] = []
    label_absolute: list[str] = []
    label_increment: list[str] = []
    for entry in entries:
        match entry:
            case Iopath() | Port() | Interconnect() | Device() | NetDelay():
                match entry.mode:
                    case DelayMode.ABSOLUTE:
                        absolute.append(format_delay_entry(entry))
                    case DelayMode.INCREMENT:
                        increment.append(format_delay_entry(entry))
                    case _:
                        assert_never(entry.mode)
            case PathPulse() | PathPulsePercent():
                pulses.append(f"{_DELAY_MODE_INDENT}{format_pulse_entry(entry)}")
            case (
                Setup()
                | Hold()
                | Recovery()
                | Removal()
                | SetupHold()
                | Recrem()
                | Skew()
                | NoChange()
                | Width()
                | Period()
            ):
                checks.append(format_timing_check(entry))
            case (
                PathConstraint()
                | PeriodConstraint()
                | SkewConstraint()
                | Arrival()
                | Departure()
                | Slack()
                | Waveform()
            ):
                constraints.append(format_timing_env(entry))
            case Label():
                line = f"({entry.name} {format_delay(entry.delay)})"
                match entry.mode:
                    case DelayMode.ABSOLUTE:
                        label_absolute.append(line)
                    case DelayMode.INCREMENT:
                        label_increment.append(line)
                    case _:
                        assert_never(entry.mode)
            case _:
                assert_never(entry)
    modes = [
        *_block("ABSOLUTE", absolute, indent=_DELAY_MODE_INDENT, body=_ENTRY_INDENT),
        *_block("INCREMENT", increment, indent=_DELAY_MODE_INDENT, body=_ENTRY_INDENT),
        *pulses,
    ]
    label_modes = [
        *_block(
            "ABSOLUTE", label_absolute, indent=_DELAY_MODE_INDENT, body=_ENTRY_INDENT
        ),
        *_block(
            "INCREMENT", label_increment, indent=_DELAY_MODE_INDENT, body=_ENTRY_INDENT
        ),
    ]
    return [
        *_block("DELAY", modes, indent=_BLOCK_INDENT, body=""),
        *_block("TIMINGCHECK", checks, indent=_BLOCK_INDENT, body=_ENTRY_INDENT),
        *_block("TIMINGENV", constraints, indent=_BLOCK_INDENT, body=_ENTRY_INDENT),
        *_block("LABEL", label_modes, indent=_BLOCK_INDENT, body=""),
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
