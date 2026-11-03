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

"""Timescale conversion helpers."""

from sdf_toolkit.core.model import Timescale


def get_scale_fs(timescale: Timescale) -> int:
    """Return the length of one *timescale* step in femtoseconds.

    >>> get_scale_fs(Timescale.parse("1.0 fs"))
    1
    >>> get_scale_fs(Timescale.parse("1ps"))
    1000
    >>> get_scale_fs(Timescale.parse("10 ns"))
    10000000
    >>> get_scale_fs(Timescale.parse("10.0 us"))
    10000000000
    >>> get_scale_fs(Timescale.parse("100.0ms"))
    100000000000000
    >>> get_scale_fs(Timescale.parse("100 s"))
    100000000000000000
    """
    return timescale.femtoseconds


def get_scale_seconds(timescale: Timescale) -> float:
    """Return the length of one *timescale* step in seconds.

    >>> get_scale_seconds(Timescale.parse("1.0 fs"))
    1e-15
    >>> get_scale_seconds(Timescale.parse("1ps"))
    1e-12
    >>> get_scale_seconds(Timescale.parse("10 ns"))
    1e-08
    >>> get_scale_seconds(Timescale.parse("100.0ms"))
    0.1
    """
    return 1e-15 * timescale.femtoseconds
