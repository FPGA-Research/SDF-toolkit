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
"""Legacy public API for parsing and emitting SDF timing files.

Parsing is now implemented in {mod}`sdf_toolkit.parser`.  The ``parse``
function here delegates to {func}`sdf_toolkit.parser.parse_sdf` for
backward compatibility.
"""

import json
import sys
from pathlib import Path

from sdf_toolkit.core.model import SDFFile
from sdf_toolkit.io import writer
from sdf_toolkit.parser.parser import parse_sdf


def emit(input: SDFFile, timescale: str = "1ps") -> str:  # noqa: A002
    """Emit SDF content from a parsed timing data structure.

    Parameters
    ----------
    input : SDFFile
        The parsed SDF file to emit.
    timescale : str
        The timescale to use in the output.

    Returns
    -------
    str
        The SDF file content as a string.

    Examples
    --------
    >>> from sdf_toolkit.io.sdfparse import parse, emit
    >>> sdf_text = '(DELAYFILE (SDFVERSION "3.0") (TIMESCALE 1ps))'
    >>> sdf = parse(sdf_text)
    >>> output = emit(sdf)
    >>> "(SDFVERSION" in output and "3.0" in output
    True
    """
    return writer.emit_sdf(input, timescale, header=input.header)


def parse(input: str, *, workers: int | None = None) -> SDFFile:  # noqa: A002
    """Parse SDF input text and return an SDFFile.

    Parameters
    ----------
    input : str
        The raw SDF file content as a string.
    workers : int | None
        Number of worker processes to parse the cell blocks in, as in
        {func}`sdf_toolkit.parser.parse_sdf`.

    Returns
    -------
    SDFFile
        The parsed SDF file object.

    Examples
    --------
    >>> from sdf_toolkit.io.sdfparse import parse
    >>> sdf_text = '(DELAYFILE (SDFVERSION "3.0") (TIMESCALE 1ps))'
    >>> sdf = parse(sdf_text)
    >>> sdf.header.sdfversion
    '3.0'
    >>> sdf.header.timescale
    '1ps'
    """
    return parse_sdf(input, workers=workers)


def main() -> None:
    """Run the command line SDF parser."""
    if len(sys.argv) != 2:
        print("Usage: sdf_toolkit_parse <sdf_file>")  # noqa: T201
        sys.exit(1)

    sdf_file = sys.argv[1]

    try:
        content = Path(sdf_file).read_text()
        result = parse(content)
        print(json.dumps(result.to_dict(), indent=2))  # noqa: T201
    except Exception as e:  # noqa: BLE001
        print(f"Error parsing SDF file: {e}")  # noqa: T201
        sys.exit(1)
