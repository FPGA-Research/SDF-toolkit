"""Parse, round-trip and reject each SDF 3.0 construct on its own.

Every case is the body of one ``(CELL ...)``, the entries it must parse to,
and a malformed variant with the error it must raise.
"""

from dataclasses import dataclass
from decimal import Decimal

import pytest

from sdf_toolkit.core.checks import (
    BidirectSkew,
    NoChange,
    Recrem,
    Setup,
    SetupHold,
    Skew,
    Width,
)
from sdf_toolkit.core.delays import (
    Cond,
    Device,
    Iopath,
    NetDelay,
    PathPulse,
    PathPulsePercent,
    PortSpec,
    Retain,
    TimingPortSpec,
)
from sdf_toolkit.core.enums import DelayMode, EdgeType
from sdf_toolkit.core.sdffile import Entry, Label, entry_from_dict, entry_to_dict
from sdf_toolkit.core.timingenv import (
    Arrival,
    ConstraintPath,
    Departure,
    DiffConstraint,
    PathConstraint,
    PeriodConstraint,
    SkewConstraint,
    Slack,
    SumConstraint,
    Waveform,
    WaveformEdge,
)
from sdf_toolkit.core.values import DelayValues, PulseLimits, Values
from sdf_toolkit.io.writer import emit_sdf
from sdf_toolkit.parser import SDFParseError
from sdf_toolkit.parser.errors import UnsupportedConstructError
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
_POSCLK = TimingPortSpec(port="clk", edge=EdgeType.POSEDGE)

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
        id="padded-delval-list",
        body=(
            "(DELAY (ABSOLUTE (IOPATH a y (1) (2) (3) (4))"
            " (IOPATH b y (1) (2) (3) (4) (5) (6) (7))))"
        ),
        entries=[
            Iopath(
                input=_A,
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(*(_v(n) for n in range(1, 5)), Values(), Values()),
            ),
            Iopath(
                input=PortSpec(port="b"),
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(
                    *(_v(n) for n in range(1, 8)), *(Values() for _ in range(5))
                ),
            ),
        ],
        malformed=f"(DELAY (ABSOLUTE (IOPATH a y {'(1)' * 13})))",
        error=ValueError,
        match="rvalues",
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
        body=(
            "(DELAY (ABSOLUTE (IOPATH a y (RETAIN (0.1) (0.2))"
            " (RETAIN ((0.3) (0.05))) (1) (2))))"
        ),
        entries=[
            Iopath(
                input=_A,
                output=_Y,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1), _v(2)),
                retain=(
                    Retain(delay=DelayValues.of(_v(0.1), _v(0.2))),
                    Retain(
                        delay=DelayValues(
                            rvalues=(_v(0.3),), limits=(PulseLimits(reject=_v(0.05)),)
                        )
                    ),
                ),
            )
        ],
        malformed="(DELAY (ABSOLUTE (IOPATH a y (RETAIN (1) (2) (3) (4)) (1))))",
        error=ValueError,
        match="RETAIN takes 1 to 3 delvals",
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
    Case(
        id="device-without-port",
        body="(DELAY (ABSOLUTE (DEVICE (1:3:8) (4:5:7)) (DEVICE q (1))))",
        entries=[
            Device(
                output=None,
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(Values(1, 3, 8), Values(4, 5, 7)),
            ),
            Device(
                output=PortSpec(port="q"),
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1)),
            ),
        ],
        # port_instance takes no edge.
        malformed="(DELAY (ABSOLUTE (DEVICE (posedge q) (1))))",
    ),
    Case(
        id="recrem",
        body="(TIMINGCHECK (RECREM (posedge rst) (posedge clk) (1) (2)))",
        entries=[
            Recrem(
                data=TimingPortSpec(port="rst", edge=EdgeType.POSEDGE),
                reference=_POSCLK,
                recovery=_v(1),
                removal=_v(2),
            )
        ],
        # The form with SCOND or CCOND takes port_spec, not port_tchk.
        malformed="(TIMINGCHECK (RECREM (COND en r) clk (1) (2) (SCOND en)))",
        error=ValueError,
        match="takes plain ports",
    ),
    Case(
        id="skew",
        body="(TIMINGCHECK (SKEW (posedge c1) (negedge c2) (0.5)))",
        entries=[
            Skew(
                stamp=TimingPortSpec(port="c1", edge=EdgeType.POSEDGE),
                check=TimingPortSpec(port="c2", edge=EdgeType.NEGEDGE),
                limit=_v(0.5),
            )
        ],
        malformed="(TIMINGCHECK (SKEW c1 (0.5)))",
    ),
    Case(
        id="bidirectskew",
        body="(TIMINGCHECK (BIDIRECTSKEW (posedge c1) (posedge c2) (6) (7)))",
        entries=[
            BidirectSkew(
                first=TimingPortSpec(port="c1", edge=EdgeType.POSEDGE),
                second=TimingPortSpec(port="c2", edge=EdgeType.POSEDGE),
                first_limit=_v(6),
                second_limit=_v(7),
            )
        ],
        malformed="(TIMINGCHECK (BIDIRECTSKEW c1 c2 (6)))",
    ),
    Case(
        id="nochange",
        body="(TIMINGCHECK (NOCHANGE (negedge we) addr (1) (2)))",
        entries=[
            NoChange(
                control=TimingPortSpec(port="we", edge=EdgeType.NEGEDGE),
                data=TimingPortSpec(port="addr"),
                setup=_v(1),
                hold=_v(2),
            )
        ],
        # OVI SDF 3.0 p.3-35 requires an edge on the control port.
        malformed="(TIMINGCHECK (NOCHANGE we addr (1) (2)))",
        error=ValueError,
        match="NOCHANGE control port we needs an edge",
    ),
    Case(
        id="scond-ccond",
        body=(
            "(TIMINGCHECK"
            ' (SETUPHOLD d (posedge clk) (1) (2) (SCOND "s1" en) (CCOND !rst))'
            " (RECREM r (posedge clk) (3) (4) (CCOND en)))"
        ),
        entries=[
            SetupHold(
                data=TimingPortSpec(port="d"),
                reference=_POSCLK,
                setup=_v(1),
                hold=_v(2),
                scond=Cond(equation="en", name="s1"),
                ccond=Cond(equation="! rst"),
            ),
            Recrem(
                data=TimingPortSpec(port="r"),
                reference=_POSCLK,
                recovery=_v(3),
                removal=_v(4),
                ccond=Cond(equation="en"),
            ),
        ],
        # SCOND comes before CCOND.
        malformed="(TIMINGCHECK (SETUPHOLD d clk (1) (2) (CCOND en) (SCOND en)))",
    ),
    Case(
        id="pathconstraint-name-and-ports",
        body=(
            '(TIMINGENV (PATHCONSTRAINT (NAME "p1") a u1/b y (1) (2))'
            " (PATHCONSTRAINT (NAME) a y (3) (4)))"
        ),
        entries=[
            PathConstraint(name="p1", ports=("a", "u1/b", "y"), rise=_v(1), fall=_v(2)),
            PathConstraint(name="", ports=("a", "y"), rise=_v(3), fall=_v(4)),
        ],
        malformed="(TIMINGENV (PATHCONSTRAINT a (1) (2)))",
    ),
    Case(
        id="sum",
        body=(
            "(TIMINGENV (SUM (a b) (c d) (e f) (6))"
            " (SUM (m.n.o1 y.z.i1) (y.z.o2 a.b.i2) (67.3) (70)))"
        ),
        entries=[
            SumConstraint(
                paths=(
                    ConstraintPath(start="a", end="b"),
                    ConstraintPath(start="c", end="d"),
                    ConstraintPath(start="e", end="f"),
                ),
                rise=_v(6),
            ),
            SumConstraint(
                paths=(
                    ConstraintPath(start="m.n.o1", end="y.z.i1"),
                    ConstraintPath(start="y.z.o2", end="a.b.i2"),
                ),
                rise=_v(67.3),
                fall=_v(70),
            ),
        ],
        malformed="(TIMINGENV (SUM (a b) (6)))",
    ),
    Case(
        id="diff",
        body="(TIMINGENV (DIFF (a b) (c d) (8.3)) (DIFF (a b) (c d) (1) (2)))",
        entries=[
            DiffConstraint(
                paths=(
                    ConstraintPath(start="a", end="b"),
                    ConstraintPath(start="c", end="d"),
                ),
                rise=_v(8.3),
            ),
            DiffConstraint(
                paths=(
                    ConstraintPath(start="a", end="b"),
                    ConstraintPath(start="c", end="d"),
                ),
                rise=_v(1),
                fall=_v(2),
            ),
        ],
        malformed="(TIMINGENV (DIFF (a b) (c d) (e f) (1)))",
    ),
    Case(
        id="periodconstraint",
        body=(
            "(TIMINGENV (PERIODCONSTRAINT clk (10))"
            " (PERIODCONSTRAINT u1/ck (8) (EXCEPTION (INSTANCE u2) (INSTANCE u3))))"
        ),
        entries=[
            PeriodConstraint(port="clk", limit=_v(10)),
            PeriodConstraint(port="u1/ck", limit=_v(8), exception=("u2", "u3")),
        ],
        malformed="(TIMINGENV (PERIODCONSTRAINT clk (10) (EXCEPTION)))",
    ),
    Case(
        id="skewconstraint",
        body="(TIMINGENV (SKEWCONSTRAINT (posedge clk) (0.2)))",
        entries=[
            SkewConstraint(
                port=PortSpec(port="clk", edge=EdgeType.POSEDGE), limit=_v(0.2)
            )
        ],
        malformed="(TIMINGENV (SKEWCONSTRAINT clk (0.2) (0.3)))",
    ),
    Case(
        id="arrival-departure",
        body=(
            "(TIMINGENV (ARRIVAL (posedge clk) d (1) (2) (3) (4))"
            " (DEPARTURE q (5) (6) (7) (8)))"
        ),
        entries=[
            Arrival(
                reference=PortSpec(port="clk", edge=EdgeType.POSEDGE),
                port="d",
                earliest_rise=_v(1),
                latest_rise=_v(2),
                earliest_fall=_v(3),
                latest_fall=_v(4),
            ),
            Departure(
                port="q",
                earliest_rise=_v(5),
                latest_rise=_v(6),
                earliest_fall=_v(7),
                latest_fall=_v(8),
            ),
        ],
        # The reference is a port_edge, so it needs an edge.
        malformed="(TIMINGENV (ARRIVAL clk d (1) (2) (3) (4)))",
    ),
    Case(
        id="slack",
        body="(TIMINGENV (SLACK d (1) (2) (3) (4) 10) (SLACK q (1) (2) (3) (4)))",
        entries=[
            Slack(
                port="d",
                rise_setup=_v(1),
                fall_setup=_v(2),
                rise_hold=_v(3),
                fall_hold=_v(4),
                period=Decimal(10),
            ),
            Slack(
                port="q",
                rise_setup=_v(1),
                fall_setup=_v(2),
                rise_hold=_v(3),
                fall_hold=_v(4),
            ),
        ],
        malformed="(TIMINGENV (SLACK d (1) (2) (3)))",
    ),
    Case(
        id="waveform",
        body=(
            "(TIMINGENV (WAVEFORM clk 10 (posedge -1) (negedge 5 6)"
            " (posedge 7) (negedge 10)))"
        ),
        entries=[
            Waveform(
                port="clk",
                period=Decimal(10),
                edges=(
                    WaveformEdge(edge=EdgeType.POSEDGE, offsets=(Decimal(-1),)),
                    WaveformEdge(
                        edge=EdgeType.NEGEDGE, offsets=(Decimal(5), Decimal(6))
                    ),
                    WaveformEdge(edge=EdgeType.POSEDGE, offsets=(Decimal(7),)),
                    WaveformEdge(edge=EdgeType.NEGEDGE, offsets=(Decimal(10),)),
                ),
            )
        ],
        # Edges come in posedge and negedge pairs.
        malformed="(TIMINGENV (WAVEFORM clk 10 (posedge 0) (posedge 5)))",
        error=ValueError,
        match="alternate",
    ),
    Case(
        id="label",
        body=(
            "(LABEL (ABSOLUTE (tpd_a (1) (2)) (tw ((3) (0.5))))"
            " (INCREMENT (tpd_a (1))))"
        ),
        entries=[
            Label(
                name="tpd_a",
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues.of(_v(1), _v(2)),
            ),
            Label(
                name="tw",
                mode=DelayMode.ABSOLUTE,
                delay=DelayValues(
                    rvalues=(_v(3),), limits=(PulseLimits(reject=_v(0.5)),)
                ),
            ),
            Label(name="tpd_a", mode=DelayMode.INCREMENT, delay=DelayValues.of(_v(1))),
        ],
        malformed="(LABEL (ABSOLUTE))",
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


@pytest.mark.parametrize(
    "body",
    [
        "(DELAY (ABSOLUTE (IOPATH a (posedge y) (1))))",
        "(DELAY (ABSOLUTE (PORT (posedge a) (1))))",
        "(DELAY (ABSOLUTE (INTERCONNECT (posedge a) b (1))))",
        "(DELAY (ABSOLUTE (INTERCONNECT a (negedge b) (1))))",
        "(DELAY (ABSOLUTE))",
        "(DELAY (ABSOLUTE (IOPATH a y (::))))",
    ],
    ids=[
        "iopath-output-edge",
        "port-edge",
        "interconnect-source-edge",
        "interconnect-load-edge",
        "empty-absolute",
        "triple-without-number",
    ],
)
def test_rejects_syntax_the_spec_forbids(body: str) -> None:
    with pytest.raises(SDFParseError):
        parse_sdf(_cell(body))


@pytest.mark.parametrize(
    ("body", "match"),
    [
        (
            "(TIMINGENV (WAVEFORM clk 10 (posedge 5) (negedge 2)))",
            "offsets must increase",
        ),
        (
            "(TIMINGENV (WAVEFORM clk 10 (posedge 2 1) (negedge 5)))",
            "offsets must increase",
        ),
        (
            "(TIMINGENV (WAVEFORM clk 10 (posedge 0) (negedge 10 11)))",
            "exceeds the period",
        ),
        ("(TIMINGCHECK (WIDTH (posedge clk) (-1)))", r"WIDTH \(posedge clk\) limit"),
        ("(TIMINGCHECK (PERIOD clk (1:-2:3)))", r"PERIOD clk limit \(1:-2:3\)"),
        ("(TIMINGENV (PERIODCONSTRAINT clk (-10)))", "PERIODCONSTRAINT clk limit"),
        ("(TIMINGENV (SKEWCONSTRAINT clk (-0.2)))", "SKEWCONSTRAINT clk limit"),
        ("(DELAY (PATHPULSE a y (-1)))", "PATHPULSE a y reject limit"),
        ("(DELAY (PATHPULSE (1) (::-2)))", "PATHPULSE error limit"),
        ("(DELAY (PATHPULSEPERCENT (-25)))", "PATHPULSEPERCENT reject limit"),
        ("(TIMINGENV (SLACK d (1) (2) (3) (4) -10))", "SLACK d period"),
        (
            "(TIMINGCHECK (BIDIRECTSKEW c1 c2 (6) (-7)))",
            "BIDIRECTSKEW c1 c2 second_limit",
        ),
        ("(TIMINGENV (DIFF (a b) (c d) (1) (-2)))", "DIFF a b c d fall"),
    ],
    ids=[
        "waveform-edge-order",
        "waveform-region-order",
        "waveform-past-period",
        "width",
        "period",
        "periodconstraint",
        "skewconstraint",
        "pathpulse-reject",
        "pathpulse-error",
        "pathpulsepercent",
        "slack-period",
        "bidirectskew",
        "diff",
    ],
)
def test_rejects_values_the_spec_forbids(body: str, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        parse_sdf(_cell(body))


@pytest.mark.parametrize(
    "text",
    [
        _cell('(INCLUDE "u1.sdf")'),
        '(DELAYFILE (SDFVERSION "3.0") (INCLUDE "cells.sdf"))',
    ],
    ids=["in-cell", "in-file"],
)
def test_include_is_not_supported(text: str) -> None:
    with pytest.raises(UnsupportedConstructError, match="INCLUDE .* not supported"):
        parse_sdf(text)
