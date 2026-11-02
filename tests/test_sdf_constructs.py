"""Parse, round-trip and reject each SDF 3.0 construct on its own.

Every case is the body of one ``(CELL ...)``, the entries it must parse to,
and a malformed variant with the error it must raise.
"""

from dataclasses import dataclass

import pytest

from sdf_toolkit.core.model import (
    Cond,
    DelayMode,
    DelayValues,
    EdgeType,
    Entry,
    Iopath,
    PortSpec,
    Setup,
    TimingPortSpec,
    Values,
    entry_from_dict,
    entry_to_dict,
)
from sdf_toolkit.io.writer import emit_sdf
from sdf_toolkit.parser import SDFParseError
from sdf_toolkit.parser.parser import parse_sdf


@dataclass(frozen=True)
class Case:
    """One construct: its cell body, its model and a body that must fail."""

    id: str
    body: str
    entries: list[Entry]
    malformed: str
    error: type[Exception] = SDFParseError


def _cell(body: str) -> str:
    return f'(DELAYFILE (SDFVERSION "3.0") (CELL (CELLTYPE "C") (INSTANCE u1) {body}))'


def _v(value: float) -> Values:
    return Values.uniform(value)


_A = PortSpec(port="a")
_Y = PortSpec(port="y")

CASES = [
    Case(
        id="named-cond-iopath",
        body='(DELAY (ABSOLUTE (COND "c1" (a && b) (IOPATH a y (1) (2)))))',
        entries=[
            Iopath(
                input=_A,
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1), _v(2)),
                cond=Cond(equation="a && b", name="c1"),
            )
        ],
        # IEEE 1497 gives CONDELSE no name.
        malformed='(DELAY (ABSOLUTE (CONDELSE "c1" (IOPATH a y (1)))))',
    ),
    Case(
        id="named-cond-tchk-port",
        body='(TIMINGCHECK (SETUP (COND "en_hi" en d) (posedge clk) (1)))',
        entries=[
            Setup(
                data=TimingPortSpec(port="d", cond=Cond(equation="en", name="en_hi")),
                reference=TimingPortSpec(port="clk", edge=EdgeType.POSEDGE),
                limit=_v(1),
            )
        ],
        malformed='(TIMINGCHECK (SETUP (COND "a" "b" en d) (posedge clk) (1)))',
    ),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_parses_to_model(case: Case) -> None:
    assert parse_sdf(_cell(case.body)).cells == {"C": {"u1": case.entries}}


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_emit_then_parse_gives_equal_model(case: Case) -> None:
    parsed = parse_sdf(_cell(case.body))
    assert parse_sdf(emit_sdf(parsed)) == parsed


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_json_form_round_trips(case: Case) -> None:
    assert [entry_from_dict(entry_to_dict(e)) for e in case.entries] == case.entries


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_rejects_malformed(case: Case) -> None:
    with pytest.raises(case.error):
        parse_sdf(_cell(case.malformed))
