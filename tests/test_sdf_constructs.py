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
    NetDelay,
    PathPulse,
    PathPulsePercent,
    PortSpec,
    PulseLimits,
    Retain,
    Setup,
    TimingPortSpec,
    Values,
    Width,
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
    match: str | None = None


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
    Case(
        id="signed-rvalue",
        body="(DELAY (ABSOLUTE (IOPATH a y (+1.5) (+1:+2:+3))))",
        entries=[
            Iopath(
                input=_A,
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1.5), Values(min=1, typ=2, max=3)),
            )
        ],
        malformed="(DELAY (ABSOLUTE (IOPATH a y (+-1))))",
    ),
    Case(
        id="transition-edges",
        body=(
            "(DELAY (ABSOLUTE (IOPATH (01 a) y (1)) (IOPATH (z0 a) y (2))))"
            " (TIMINGCHECK (WIDTH (1z clk) (3)))"
        ),
        entries=[
            Iopath(
                input=PortSpec(port="a", edge=EdgeType.E01),
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1)),
            ),
            Iopath(
                input=PortSpec(port="a", edge=EdgeType.EZ0),
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(2)),
            ),
            Width(port=TimingPortSpec(port="clk", edge=EdgeType.E1Z), limit=_v(3)),
        ],
        # IEEE 1497 edge identifiers have no X transitions.
        malformed="(DELAY (ABSOLUTE (IOPATH (0x a) y (1))))",
    ),
    Case(
        id="bus-range",
        body="(DELAY (ABSOLUTE (IOPATH a[3:0] y[1] (1))))",
        entries=[
            Iopath(
                input=PortSpec(port="a[3:0]"),
                output=PortSpec(port="y[1]"),
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1)),
            )
        ],
        malformed="(DELAY (ABSOLUTE (IOPATH a[3:] y (1))))",
    ),
    Case(
        id="pulse-delval",
        body="(DELAY (ABSOLUTE (IOPATH a y ((1) (0.5)) (2) ((3) () (0.8)))))",
        entries=[
            Iopath(
                input=_A,
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues(
                    rvalues=(_v(1), _v(2), _v(3)),
                    limits=(
                        PulseLimits(reject=_v(0.5)),
                        None,
                        PulseLimits(reject=Values(), error=_v(0.8)),
                    ),
                ),
            )
        ],
        malformed="(DELAY (ABSOLUTE (IOPATH a y ((1)))))",
    ),
    Case(
        id="pathpulse",
        body="(DELAY (PATHPULSE a y (1) (2)) (PATHPULSE (3)))",
        entries=[
            PathPulse(path=("a", "y"), reject=_v(1), error=_v(2)),
            PathPulse(path=None, reject=_v(3)),
        ],
        # PATHPULSE is a deltype beside ABSOLUTE, not a del_def inside it.
        malformed="(DELAY (ABSOLUTE (PATHPULSE a y (1))))",
    ),
    Case(
        id="pathpulsepercent",
        body="(DELAY (PATHPULSEPERCENT a y (25) (35)))",
        entries=[PathPulsePercent(path=("a", "y"), reject=_v(25), error=_v(35))],
        malformed="(DELAY (PATHPULSEPERCENT a y (25) (35) (45)))",
    ),
    Case(
        id="retain",
        body="(DELAY (ABSOLUTE (IOPATH a y (RETAIN (0.1) (0.2)) (1) (2))))",
        entries=[
            Iopath(
                input=_A,
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1), _v(2)),
                retain=(Retain(rvalues=(_v(0.1), _v(0.2))),),
            )
        ],
        malformed="(DELAY (ABSOLUTE (IOPATH a y (RETAIN (1) (2) (3) (4)) (1))))",
        error=ValueError,
        match="RETAIN takes 1 to 3 rvalues",
    ),
    Case(
        id="netdelay",
        body="(DELAY (INCREMENT (NETDELAY u2/n1 (1) (2))))",
        entries=[
            NetDelay(
                net="u2/n1",
                mode=DelayMode.INCREMENT,
                delay=DelayValues.of(_v(1), _v(2)),
            )
        ],
        malformed="(DELAY (ABSOLUTE (NETDELAY (posedge n1) (1))))",
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
    with pytest.raises(case.error, match=case.match):
        parse_sdf(_cell(case.malformed))
