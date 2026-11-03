"""Tests for the per-standard rules of a parse."""

import pytest

from sdf_toolkit.core.model import Iopath, Values
from sdf_toolkit.io import emit_sdf
from sdf_toolkit.parser import (
    SDFStandard,
    StandardMismatchError,
    StandardViolationError,
    parse_sdf,
)

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
    pytest.param(
        _file(_IOPATH, header='(DESIGN "a\\"b")'),
        'an escaped double quote \\" in a QSTRING',
        id="escaped-quote",
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


@pytest.mark.parametrize(
    ("version", "declared"),
    [
        ("3.0", OVI),
        ("4.0", IEEE),
        ("IEEE 1497 4.0", IEEE),
        ("OVI 3.0", OVI),
        ("2.1", None),
        ("OVI 1.0", None),
        ("draft", None),
        # The first listed version in the string decides.
        ("2.1 then 4.0", None),
        ("3.0 then 4.0", OVI),
    ],
)
def test_sdfversion_names_its_standard(version: str, declared: SDFStandard | None):
    assert SDFStandard.declared_by(version) is declared


def _versioned(version: str) -> str:
    return _file(_IOPATH, header=f'(SDFVERSION "{version}")')


@pytest.mark.parametrize(
    ("version", "standard"),
    [("4.0", OVI), ("IEEE 1497 4.0", OVI), ("3.0", IEEE), ("OVI 3.0", IEEE)],
)
def test_explicit_standard_rejects_the_other_version(
    version: str, standard: SDFStandard
):
    with pytest.raises(StandardMismatchError) as raised:
        parse_sdf(_versioned(version), standard=standard)
    message = str(raised.value)
    assert standard.full_name in message
    assert standard.other.full_name in message
    assert f"--standard {standard.other}" in message


@pytest.mark.parametrize(
    ("version", "standard"),
    [
        ("3.0", OVI),
        ("4.0", IEEE),
        ("2.1", OVI),
        ("2.1", IEEE),
        ("3.0", None),
        ("4.0", None),
    ],
)
def test_version_check_passes(version: str, standard: SDFStandard | None):
    """A matching, older or unchecked version parses."""
    parse_sdf(_versioned(version), standard=standard)


def test_chunked_parse_raises_the_version_mismatch():
    """An SDFVERSION after the cells sits in a worker's chunk."""
    text = _file(_IOPATH, _IOPATH)[:-1] + '(SDFVERSION "4.0"))'
    with pytest.raises(StandardMismatchError):
        parse_sdf(text, workers=2, standard=OVI)


@pytest.mark.parametrize(
    ("design", "standard"),
    [
        pytest.param('a\\"b', None, id="escaped-quote"),
        pytest.param("a\\\\", OVI, id="escaped-backslash-at-end"),
        pytest.param("a\\_b", OVI, id="escaped-other"),
    ],
)
def test_qstring_keeps_its_escapes(design: str, standard: SDFStandard | None):
    text = _file(_IOPATH, header=f'(DESIGN "{design}")')
    sdf = parse_sdf(text, standard=standard)
    assert sdf.header.design == design
    assert parse_sdf(emit_sdf(sdf), standard=standard) == sdf


def _delay(*entries: str) -> str:
    return f"(DELAY (ABSOLUTE {' '.join(entries)}))"


_STRICT_COND = [
    pytest.param(_delay("(COND en (IOPATH a y (1)))"), id="cond-iopath"),
    pytest.param(_delay("(CONDELSE (IOPATH a y (1)))"), id="condelse-iopath"),
]

# Delay COND forms neither standard defines, with the entries the error names.
_LOOSE_COND = [
    pytest.param(_delay("(COND en (PORT a (1)))"), "COND wraps PORT", id="port"),
    pytest.param(_delay("(COND en (DEVICE (1)))"), "COND wraps DEVICE", id="device"),
    pytest.param(
        _delay("(COND en (INTERCONNECT a b (1)))"),
        "COND wraps INTERCONNECT",
        id="interconnect",
    ),
    pytest.param(
        _delay("(COND en (NETDELAY n (1)))"), "COND wraps NETDELAY", id="netdelay"
    ),
    pytest.param(
        _delay("(COND en (IOPATH a y (1)) (IOPATH b y (2)))"),
        "COND wraps IOPATH IOPATH",
        id="two-iopaths",
    ),
    pytest.param(
        _delay("(CONDELSE (PORT a (1)))"), "CONDELSE wraps PORT", id="condelse-port"
    ),
]


@pytest.mark.parametrize("body", _STRICT_COND)
def test_strict_cond_reads_one_iopath(body: str):
    parse_sdf(_file(body))


@pytest.mark.parametrize(("body", "wrapped"), _LOOSE_COND)
def test_strict_cond_rejects_other_forms(body: str, wrapped: str):
    with pytest.raises(StandardViolationError) as raised:
        parse_sdf(_file(body))
    message = str(raised.value)
    assert wrapped in message
    assert "--loose-cond" in message


@pytest.mark.parametrize("body", [case.values[0] for case in _LOOSE_COND])
def test_loose_cond_reads_other_forms(body: str):
    parse_sdf(_file(body), loose_cond=True)


@pytest.mark.parametrize("standard", [OVI, IEEE])
def test_strict_cond_applies_under_either_standard(standard: SDFStandard):
    with pytest.raises(StandardViolationError, match="--loose-cond"):
        parse_sdf(_file(_delay("(COND en (PORT a (1)))")), standard=standard)


def test_loose_cond_reaches_the_workers():
    # A worker without the option fails its chunk, and the serial re-parse
    # then accepts the file and reports a split fault instead.
    text = _file(_IOPATH, _delay("(COND en (PORT a (1)))"))
    sdf = parse_sdf(text, workers=2, loose_cond=True)
    assert set(sdf.cells["C"]) == {"u0", "u1"}
