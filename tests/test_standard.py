"""Tests for the per-standard rules of a parse."""

import pytest

from sdf_toolkit.core.model import Iopath, Values
from sdf_toolkit.parser import SDFStandard, StandardViolationError, parse_sdf

OVI = SDFStandard.OVI_3_0
IEEE = SDFStandard.IEEE_1497


def _file(*cells: str, header: str = "") -> str:
    """Wrap cell bodies in a file whose header names no SDF version."""
    blocks = "".join(
        f'(CELL (CELLTYPE "C") (INSTANCE u{n}) {body})' for n, body in enumerate(cells)
    )
    return f"(DELAYFILE {header}{blocks})"


_IOPATH = "(DELAY (ABSOLUTE (IOPATH a y (1))))"
_LABEL = "(LABEL (ABSOLUTE (tpd (1))))"
_RETAIN_4 = "(DELAY (ABSOLUTE (IOPATH a y (RETAIN (1) (2) (3) (4)) (5))))"

# Text that only IEEE 1497 defines, with the construct its error names.
IEEE_ONLY = [
    pytest.param(_file(_LABEL), "LABEL", id="label"),
    pytest.param(
        _file("(DELAY (INCREMENT (NETDELAY u1/n1 (1))))"), "NETDELAY", id="netdelay"
    ),
    *(
        pytest.param(
            _file(_IOPATH, header=f"(TIMESCALE 1 {unit})"),
            f"TIMESCALE unit {unit}",
            id=f"timescale-{unit}",
        )
        for unit in ("s", "ms", "fs")
    ),
]


@pytest.mark.parametrize(("text", "construct"), IEEE_ONLY)
def test_ovi_rejects_what_only_ieee_defines(text: str, construct: str):
    with pytest.raises(StandardViolationError) as raised:
        parse_sdf(text, standard=OVI)
    message = str(raised.value)
    assert f"{construct} is not part of OVI SDF 3.0" in message
    assert "--standard ieee-1497" in message


@pytest.mark.parametrize("standard", [None, IEEE])
@pytest.mark.parametrize(
    "text", [pytest.param(case.values[0], id=case.id) for case in IEEE_ONLY]
)
def test_ieee_reads_what_it_defines(text: str, standard: SDFStandard | None):
    parse_sdf(text, standard=standard)


@pytest.mark.parametrize("standard", [None, IEEE])
def test_ieee_limits_retain_to_three_delvals(standard: SDFStandard | None):
    with pytest.raises(StandardViolationError, match="got 4") as raised:
        parse_sdf(_file(_RETAIN_4), standard=standard)
    assert "--standard ovi-3.0" in str(raised.value)


def test_ovi_reads_retain_with_any_delval_count():
    sdf = parse_sdf(_file(_RETAIN_4), standard=OVI)
    [entry] = sdf.cells["C"]["u0"]
    assert isinstance(entry, Iopath)
    [retain] = entry.retain
    assert retain.delay.rvalues == (
        *(Values.uniform(v) for v in (1, 2, 3, 4)),
        Values(),
        Values(),
    )


@pytest.mark.parametrize(
    ("text", "standard"),
    [
        pytest.param(_file(_IOPATH, _LABEL), OVI, id="label-ovi"),
        pytest.param(_file(_IOPATH, _RETAIN_4), IEEE, id="retain-ieee"),
    ],
)
def test_chunked_parse_raises_the_violation(text: str, standard: SDFStandard):
    """A violation in a worker's chunk surfaces as itself, not as a split fault."""
    with pytest.raises(StandardViolationError):
        parse_sdf(text, workers=2, standard=standard)
