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

from sdf_toolkit.core.model import SDFFile
from sdf_toolkit.io import writer
from sdf_toolkit.parser.parser import parse_sdf
from sdf_toolkit.parser.standard import SDFStandard


def emit(input: SDFFile) -> str:  # noqa: A002
    """Emit SDF content from a parsed timing data structure.

    Parameters
    ----------
    input : SDFFile
        The parsed SDF file to emit.

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
    return writer.emit_sdf(input)


def parse(
    input: str,  # noqa: A002
    *,
    workers: int | None = None,
    standard: SDFStandard | None = None,
    loose_cond: bool = False,
) -> SDFFile:
    """Parse SDF input text and return an SDFFile.

    Parameters
    ----------
    input : str
        The raw SDF file content as a string.
    workers : int | None
        Number of worker processes to parse the cell blocks in, as in
        {func}`sdf_toolkit.parser.parse_sdf`.
    standard : SDFStandard | None
        The standard to hold the text to, as in
        {func}`sdf_toolkit.parser.parse_sdf`.
    loose_cond : bool
        Accept a loose delay ``COND``, as in
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
    >>> str(sdf.header.timescale)
    '1ps'
    """
    return parse_sdf(input, workers=workers, standard=standard, loose_cond=loose_cond)
