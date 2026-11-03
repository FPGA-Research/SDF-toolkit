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
"""SDF file writer using Jinja2 templates.

The templates hold the block layout; the entry text is built here, where the
type checker sees every field.  Entries keep the order of their cell list,
and the header writes only the fields the file sets, so a file without
``TIMESCALE`` or ``DIVIDER`` round-trips without gaining one.
"""

import jinja2

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
    Setup,
    SetupHold,
    TimingCheck,
    TimingPortSpec,
    Values,
    Width,
)

env = jinja2.Environment(
    loader=jinja2.PackageLoader("sdf_toolkit.io", "templates"),
    keep_trailing_newline=True,
)

_COND_INDENT = " " * 20


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
        case _:
            return (
                f"({keyword} {format_timing_port(entry.data)} "
                f"{format_timing_port(entry.reference)} {format_values(entry.limit)})"
            )


def format_path_constraint(entry: PathConstraint) -> str:
    """Write a path constraint with its ports in file order."""
    ports = " ".join(entry.ports)
    return (
        f"(PATHCONSTRAINT {ports} {format_values(entry.rise)} "
        f"{format_values(entry.fall)})"
    )


def emit_delay_entries(entries: list[Entry]) -> str:
    """Render the delay entries of one instance as ``ABSOLUTE``/``INCREMENT`` blocks."""
    delays = [
        e for e in entries if isinstance(e, Iopath | Port | Interconnect | Device)
    ]
    return env.get_template("delay.j2").render(
        absolute=[
            format_delay_entry(e) for e in delays if e.mode == DelayMode.ABSOLUTE
        ],
        increment=[
            format_delay_entry(e) for e in delays if e.mode == DelayMode.INCREMENT
        ],
    )


def emit_timingcheck_entries(entries: list[Entry]) -> str:
    """Render the timing checks of one instance as a ``TIMINGCHECK`` block."""
    lines = [
        format_timing_check(e)
        for e in entries
        if isinstance(e, Setup | Hold | Recovery | Removal | SetupHold | Width | Period)
    ]
    return env.get_template("timingcheck.j2").render(lines=lines)


def emit_timingenv_entries(entries: list[Entry]) -> str:
    """Render the path constraints of one instance as a ``TIMINGENV`` block."""
    lines = [
        format_path_constraint(e) for e in entries if isinstance(e, PathConstraint)
    ]
    return env.get_template("timingenv.j2").render(lines=lines)


def emit_sdf(timings: SDFFile, *, uppercase_celltype: bool = False) -> str:
    """Render a complete SDF file from parsed timing data."""
    prepared_cells = {
        cell_name: {
            instance_name: {
                "delay_entries": emit_delay_entries(entries),
                "timingcheck_entries": emit_timingcheck_entries(entries),
                "timingenv_entries": emit_timingenv_entries(entries),
            }
            for instance_name, entries in instances.items()
        }
        for cell_name, instances in timings.cells.items()
    }
    return env.get_template("sdf.j2").render(
        cells=prepared_cells,
        uppercase_celltype=uppercase_celltype,
        header=timings.header,
    )
