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
"""SDF file writer using Jinja2 templates."""

import jinja2

from sdf_toolkit.core.model import BaseEntry, SDFFile, SDFHeader

env = jinja2.Environment(
    loader=jinja2.PackageLoader("sdf_toolkit.io", "templates"),
    keep_trailing_newline=True,
)


def emit_timingenv_entries(delays: dict[str, BaseEntry]) -> str:
    """Render timing environment entries to SDF text."""
    entries = [delays[k] for k in sorted(delays) if delays[k].is_timing_env]
    template = env.get_template("timingenv.j2")
    return template.render(entries=entries)


def emit_timingcheck_entries(delays: dict[str, BaseEntry]) -> str:
    """Render timing check entries to SDF text."""
    entries = [delays[k] for k in sorted(delays) if delays[k].is_timing_check]
    template = env.get_template("timingcheck.j2")
    return template.render(entries=entries)


def emit_delay_entries(delays: dict[str, BaseEntry]) -> str:
    """Render absolute and incremental delay entries to SDF text."""
    sorted_delays = [delays[k] for k in sorted(delays)]
    absolute_entries = [d for d in sorted_delays if d.is_absolute]
    incremental_entries = [d for d in sorted_delays if d.is_incremental]

    template = env.get_template("delay.j2")
    return template.render(
        absolute_entries=absolute_entries, incremental_entries=incremental_entries
    )


def emit_sdf(
    timings: SDFFile,
    timescale: str = "1ps",
    uppercase_celltype: bool = False,
    header: SDFHeader | None = None,
) -> str:
    """Render a complete SDF file from parsed timing data."""
    prepared_cells = {
        cell_name: {
            instance_name: {
                "delay_entries": emit_delay_entries(delays),
                "timingcheck_entries": emit_timingcheck_entries(delays),
                "timingenv_entries": emit_timingenv_entries(delays),
            }
            for instance_name, delays in instances.items()
        }
        for cell_name, instances in timings.cells.items()
    }

    template = env.get_template("sdf.j2")
    return template.render(
        timescale=timescale,
        cells=prepared_cells,
        uppercase_celltype=uppercase_celltype,
        header=header,
    )
