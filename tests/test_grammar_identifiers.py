"""Tests for the character set accepted in unquoted SDF names.

Unquoted names (instance paths, port specs, interconnect endpoints) are matched
by the ``STRING`` terminal. They must accept the characters that real netlist
tools emit, in particular ``$`` from Verilog identifiers.
"""

import pytest

from sdf_toolkit.parser.parser import parse_sdf


def _wrap(interconnect: str) -> str:
    """Wrap a single INTERCONNECT entry in a minimal valid SDF file."""
    return (
        '(DELAYFILE (SDFVERSION "3.0") (DIVIDER /) (TIMESCALE 1 ns)'
        ' (CELL (CELLTYPE "top") (INSTANCE *)'
        " (DELAY (ABSOLUTE"
        f" {interconnect}"
        "))))"
    )


def test_dollar_in_unquoted_name():
    r"""A ``$`` is a valid Verilog identifier character.

    Yosys emits escaped identifiers such as ``LUT_flop\$_DFFE_PP_`` for internal
    flip-flop/latch cells, and STA tools (OpenSTA) propagate those names into the
    SDF unquoted. The parser must accept ``$`` in a path name rather than failing
    with ``No terminal matches '$'``.
    """
    result = parse_sdf(
        _wrap(r"(INTERCONNECT clk inst/LUT_flop\$_DFFE_PP_/CLK (0.0::0.0))")
    )
    assert len(result.cells) == 1


@pytest.mark.parametrize(
    "name",
    [
        "simple_net",
        "hier/path/to/pin",
        "bus[3]",
        r"\escaped$name",
        r"cell$_DLATCH_P_/Q",
    ],
)
def test_accepted_unquoted_names(name: str):
    """A range of identifier shapes (hierarchy, bus index, ``$``) parse."""
    result = parse_sdf(_wrap(f"(INTERCONNECT a {name} (1.0::1.0))"))
    assert len(result.cells) == 1
