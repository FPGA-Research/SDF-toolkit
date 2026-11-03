"""Tests for sdf_transformers.py -- transformer coverage for edge cases."""

import pytest
from conftest import DATA_DIR
from lark import Token

from sdf_toolkit.core.model import (
    Cond,
    CondElse,
    DelayMode,
    Device,
    EdgeType,
    Entry,
    Hold,
    Iopath,
    PathConstraint,
    Period,
    Port,
    PortSpec,
    Recovery,
    Setup,
    SetupHold,
    Timescale,
    TimeUnit,
    TimingPortSpec,
    Values,
    Width,
    labelled_values,
)
from sdf_toolkit.io import sdfparse
from sdf_toolkit.parser import SDFParseError
from sdf_toolkit.parser.parser import get_parser, parse_sdf
from sdf_toolkit.parser.transformers import SDFTransformer

_CELL_HEAD = (
    '(DELAYFILE (SDFVERSION "3.0") (DIVIDER /) (TIMESCALE 1 ns)'
    ' (CELL (CELLTYPE "top") (INSTANCE u1)'
)


def _wrap_delay(entry: str) -> str:
    """Wrap a single delay entry in a minimal valid SDF file."""
    return f"{_CELL_HEAD} (DELAY (ABSOLUTE {entry}))))"


def _wrap_timing_check(check: str) -> str:
    """Wrap a single timing check in a minimal valid SDF file."""
    return f"{_CELL_HEAD} (TIMINGCHECK {check})))"


def _wrap_cond_iopath(equation: str) -> str:
    """Wrap an IOPATH under a ``cond_delay`` COND carrying ``equation``."""
    return _wrap_delay(f"(COND ({equation}) (IOPATH A Z (1.0:2.0:3.0)))")


def _wrap_cond_setup(equation: str) -> str:
    """Wrap a SETUP whose reference ``timing_port`` COND carries ``equation``.

    The shape is the one spec example 2 uses; the equation/port_spec boundary
    here is LALR-decided, so it is copied rather than invented.
    """
    return _wrap_timing_check(f"(SETUP D (COND {equation} (posedge CP)) (1:1:1))")


def _wrap_cond_data_setup(equation: str) -> str:
    """Wrap a SETUP whose data ``timing_port``, SDF's first port, has the COND."""
    return _wrap_timing_check(f"(SETUP (COND {equation} (posedge D)) CP (1:1:1))")


def _round_trip(sdf_text: str) -> list[Entry]:
    """Parse, emit and re-parse, returning the entries of the one instance."""
    return parse_sdf(sdfparse.emit(parse_sdf(sdf_text))).cells["top"]["u1"]


def _only_entry(sdf_text: str) -> Entry:
    """Return the single entry of a file built by one of the wrappers above."""
    (entry,) = parse_sdf(sdf_text).cells["top"]["u1"]
    return entry


def _only_entry_values(sdf_text: str) -> Values:
    """Return the first value triple of that single entry."""
    return labelled_values(_only_entry(sdf_text))[0][1]


# ``equation`` joins its tokens with a single space, so these are written in the
# spaced form and each one is expected back verbatim.
_COND_EQUATIONS = [
    "B == 1'b0",
    "B == 'b1",
    "TE == 0",
    "TE == 1",
    "X == 1.5",
    "Y == -1",
    "Z == 1e3",
    "B == 1'b0 && C == 1'b0",
]


class TestIncrementDelays:
    def test_increment_delays(self):
        sdf_content = (DATA_DIR / "spec-example3.sdf").read_text()
        instance = parse_sdf(sdf_content).cells["XOR2"]["top.x1"]
        for entry in instance:
            assert isinstance(entry, Iopath)
            assert entry.mode == DelayMode.INCREMENT


class TestCondTimingChecks:
    def test_cond_timing_checks(self):
        sdf_content = (DATA_DIR / "spec-example2.sdf").read_text()
        instance = parse_sdf(sdf_content).cells["CDS_GEN_FD_P_SD_RB_SB_NO"]["top.ff1"]

        setups = [e for e in instance if isinstance(e, Setup)]
        holds = [e for e in instance if isinstance(e, Hold)]
        recoveries = [e for e in instance if isinstance(e, Recovery)]
        widths = [e for e in instance if isinstance(e, Width)]
        setupholds = [e for e in instance if isinstance(e, SetupHold)]

        assert len(setups) == 1
        assert len(holds) == 1
        assert len(recoveries) == 2
        assert len(setupholds) == 1

        # (SETUP D (COND D_ENABLE (posedge CP)) ...): D is data, CP reference.
        for entry in setups + holds:
            assert entry.data == TimingPortSpec("D")
            assert entry.reference == TimingPortSpec(
                "CP", edge=EdgeType.POSEDGE, cond=Cond("D_ENABLE")
            )

        assert sum(w.port.cond is not None for w in widths) == 2
        assert sum(w.port.cond is None for w in widths) == 2

    @pytest.mark.parametrize("equation", _COND_EQUATIONS)
    def test_timing_port_cond_equation_text_preserved(self, equation: str):
        """``timing_port`` is a separate COND path and preserves text the same way."""
        entry = _only_entry(_wrap_cond_setup(equation))
        assert isinstance(entry, Setup)
        assert entry.reference.cond == Cond(equation)
        assert entry.data.cond is None

    @pytest.mark.parametrize("equation", _COND_EQUATIONS)
    def test_cond_on_data_port_kept(self, equation: str):
        """A COND on the first (data) port stays on the data port."""
        entry = _only_entry(_wrap_cond_data_setup(equation))
        assert isinstance(entry, Setup)
        assert entry.data == TimingPortSpec(
            "D", edge=EdgeType.POSEDGE, cond=Cond(equation)
        )
        assert entry.reference == TimingPortSpec("CP")

    @pytest.mark.parametrize("equation", _COND_EQUATIONS)
    def test_cond_on_data_port_round_trips(self, equation: str):
        """The writer emits the data-port COND, so a re-parse gives the same entry."""
        original = _only_entry(_wrap_cond_data_setup(equation))
        emitted = sdfparse.emit(parse_sdf(_wrap_cond_data_setup(equation)))
        (reparsed,) = parse_sdf(emitted).cells["top"]["u1"]
        assert reparsed == original


class TestCondIopathCollisions:
    def test_all_conditional_iopaths_preserved(self):
        """All 4 conditional IOPATHs for CP->Q/QN must survive, not just 2."""
        sdf_content = (DATA_DIR / "spec-example2.sdf").read_text()
        instance = parse_sdf(sdf_content).cells["CDS_GEN_FD_P_SD_RB_SB_NO"]["top.ff1"]
        cond_iopaths = [
            e for e in instance if isinstance(e, Iopath) and e.cond is not None
        ]
        assert len(cond_iopaths) == 4

    def test_all_hold_setup_entries_preserved(self):
        """clb.sdf has multiple HOLD/SETUP CLK entries that must all survive."""
        sdf_content = (DATA_DIR / "clb.sdf").read_text()
        instance = parse_sdf(sdf_content).cells["LUT_OR_MEM5LRAM"]["SLICEM"]
        assert sum(isinstance(e, Hold) for e in instance) == 6
        assert sum(isinstance(e, Setup) for e in instance) == 6


class TestSingleFloatRvalue:
    """IEEE 1497 allows a scalar wherever a triple is, and it applies to all
    three of min:typ:max. ``rvalue`` is shared by every delay and timing check,
    so a missing scalar alternative rejects the whole file, not one entry.
    """

    @pytest.mark.parametrize(
        ("rvalue", "expected"),
        [
            ("(5)", Values(min=5.0, typ=5.0, max=5.0)),
            ("(-2.5)", Values(min=-2.5, typ=-2.5, max=-2.5)),
            ("(1e3)", Values(min=1000.0, typ=1000.0, max=1000.0)),
            ("5", Values(min=5.0, typ=5.0, max=5.0)),
            ("()", Values(min=None, typ=None, max=None)),
            ("(1.0:2.0:3.0)", Values(min=1.0, typ=2.0, max=3.0)),
            ("(1::)", Values(min=1.0)),
            ("(:2:)", Values(typ=2.0)),
            ("(:2:3)", Values(typ=2.0, max=3.0)),
            ("(::3)", Values(max=3.0)),
        ],
    )
    def test_iopath_rvalue_forms(self, rvalue: str, expected: Values):
        """A scalar fills min, typ and max; the empty and triple forms are unchanged."""
        assert _only_entry_values(_wrap_delay(f"(IOPATH A Z {rvalue})")) == expected

    @pytest.mark.parametrize(
        ("check", "expected"),
        [
            ("(SETUP A (posedge CP) (3))", Values(min=3.0, typ=3.0, max=3.0)),
            ("(HOLD A (posedge CP) (0.5))", Values(min=0.5, typ=0.5, max=0.5)),
            ("(WIDTH (posedge CP) (2.5))", Values(min=2.5, typ=2.5, max=2.5)),
            ("(PERIOD (posedge CP) (10))", Values(min=10.0, typ=10.0, max=10.0)),
        ],
    )
    def test_timing_check_rvalue_scalar(self, check: str, expected: Values):
        """Timing checks share ``rvalue``, so the scalar form applies there too."""
        assert _only_entry_values(_wrap_timing_check(check)) == expected

    def test_scalar_round_trips_as_full_triple(self):
        """A scalar emits as ``(5.0:5.0:5.0)``, not the min/max-dropping ``(:5.0:)``.

        An entry with no min or max is dropped from critical-path and stats
        reporting, so the scalar has to reach all three fields on parse.
        """
        emitted = sdfparse.emit(parse_sdf(_wrap_delay("(IOPATH A Z (5))")))
        assert "(5.0:5.0:5.0)" in emitted
        assert "(:5.0:)" not in emitted


class TestRvalueListPositions:
    """The position of an rvalue in the list is its transition, not a corner."""

    @pytest.mark.parametrize("count", [1, 2, 3, 6, 12])
    def test_delval_list_keeps_every_rvalue_in_order(self, count: int):
        rvalues = " ".join(f"({i}:{i}:{i})" for i in range(1, count + 1))
        entry = _only_entry(_wrap_delay(f"(IOPATH A Z {rvalues})"))
        assert isinstance(entry, Iopath)
        assert entry.delay.rvalues == tuple(
            Values.uniform(float(i)) for i in range(1, count + 1)
        )

    def test_invalid_rvalue_count_rejected(self):
        with pytest.raises(ValueError, match="rvalues"):
            parse_sdf(_wrap_delay("(IOPATH A Z (1) (2) (3) (4))"))


class TestConditionalDelays:
    def test_cond_iopath_with_equation(self):
        sdf_content = (DATA_DIR / "fixpoint.sdf").read_text()
        instance = parse_sdf(sdf_content).cells["routing_bel"]["slicem/lut_c"]
        for entry in instance:
            assert isinstance(entry, Iopath)
            assert isinstance(entry.cond, Cond)
            assert entry.cond.equation

    def test_cond_increment_with_equation(self):
        sdf_content = (DATA_DIR / "spec-example3.sdf").read_text()
        instance = parse_sdf(sdf_content).cells["XOR2"]["top.x1"]
        cond_entries = [
            e for e in instance if isinstance(e, Iopath) and e.cond is not None
        ]
        assert cond_entries
        for entry in cond_entries:
            assert isinstance(entry.cond, Cond)

    @pytest.mark.parametrize("equation", _COND_EQUATIONS)
    def test_cond_equation_text_preserved(self, equation: str):
        """A condition survives the parse as written.

        Scalar constants used to split, because ``SCALARCONSTANT`` has an
        optional leading digit, so ``1'b0`` lexed as a number plus ``'b0`` and
        rejoined as ``1.0 'b0``. Plain integers were converted by the ``FLOAT``
        callback, so ``TE == 0`` became ``TE == 0.0``.
        """
        entry = _only_entry(_wrap_cond_iopath(equation))
        assert isinstance(entry, Iopath)
        assert entry.cond == Cond(equation)

    @pytest.mark.parametrize("equation", _COND_EQUATIONS)
    def test_emitted_cond_keeps_equation_text(self, equation: str):
        """The written file carries the original condition, not a converted one."""
        emitted = sdfparse.emit(parse_sdf(_wrap_cond_iopath(equation)))
        assert f"(COND ({equation})" in emitted

    def test_delay_values_after_cond_still_lex_as_float(self):
        """A condition must not capture the delay positions that follow it.

        The contextual lexer is what keeps ``EQNUMBER`` and ``FLOAT`` apart, so
        the two have to be checked in the same file.
        """
        entry = _only_entry(_wrap_cond_iopath("TE == 0"))
        assert isinstance(entry, Iopath)
        assert entry.delay.rvalues == (Values(min=1.0, typ=2.0, max=3.0),)


class TestPathConstraints:
    def test_pathconstraint(self):
        sdf_content = (DATA_DIR / "spec-example4.sdf").read_text()
        cells = parse_sdf(sdf_content).cells
        assert "XOR" in cells
        for instance in cells["XOR"].values():
            for entry in instance:
                assert isinstance(entry, PathConstraint)
                assert not entry.rise.is_empty()
                assert not entry.fall.is_empty()


class TestPortDelays:
    def test_port_delays(self):
        sdf_content = (DATA_DIR / "spec-example2.sdf").read_text()
        instance = parse_sdf(sdf_content).cells["CDS_GEN_FD_P_SD_RB_SB_NO"]["top.ff1"]
        assert any(isinstance(e, Port) for e in instance)


class TestDeviceDelays:
    def test_device_delays(self):
        sdf_content = (DATA_DIR / "test-device.sdf").read_text()
        for instances in parse_sdf(sdf_content).cells.values():
            for entries in instances.values():
                for entry in entries:
                    assert isinstance(entry, Device)


class TestEmptyRvalue:
    def test_empty_rvalue_produces_default_values(self):
        """An rvalue ``()`` gives no value at any corner."""
        assert SDFTransformer().empty_rvalue([]) == Values()


class TestInvalidPortSpec:
    def test_invalid_port_spec_raises(self):
        """port_spec with >2 args raises ValueError."""
        t = SDFTransformer()
        with pytest.raises(ValueError, match="Invalid port_spec"):
            t.port_spec(
                Token("ID", "posedge"),
                Token("ID", "CLK"),
                Token("ID", "extra"),
            )


class TestPeriodCheck:
    PERIOD_SDF = """(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "top")
  (DIVIDER /)
  (TIMESCALE 1.0 ns)
  (CELL
    (CELLTYPE "dff")
    (INSTANCE ff0)
    (TIMINGCHECK
      (SETUP (posedge D) (posedge CLK) (0.118::0.118))
      (WIDTH (posedge CLK) (0.495::0.495))
      (PERIOD CLK (1.058::1.058))
      (PERIOD (posedge CLK2) (2.0::2.5))
    )
  )
)"""

    def _periods(self, text: str) -> list[Period]:
        entries = parse_sdf(text).cells["dff"]["ff0"]
        return [e for e in entries if isinstance(e, Period)]

    def test_period_check_parsed(self):
        """OpenSTA-style PERIOD checks parse into Period entries."""
        plain, edged = self._periods(self.PERIOD_SDF)
        assert plain.port == TimingPortSpec("CLK")
        assert plain.limit == Values(min=1.058, max=1.058)
        assert edged.port == TimingPortSpec("CLK2", edge=EdgeType.POSEDGE)
        assert edged.limit == Values(min=2.0, max=2.5)

    def test_period_check_round_trip(self):
        """Emitting a parsed file reproduces the PERIOD check shape."""
        text = sdfparse.emit(parse_sdf(self.PERIOD_SDF))
        assert "PERIOD" in text
        assert self._periods(text) == self._periods(self.PERIOD_SDF)


_IOPATH = "(IOPATH A Z (1.0:2.0:3.0))"


class TestCondElse:
    """``CONDELSE`` is its own keyword, not ``COND`` with the equation ELSE."""

    @pytest.mark.parametrize(
        "entry",
        [
            f"(CONDELSE {_IOPATH})",
            f"(CONDELSE\n{_IOPATH})",
            f"(CONDELSE/* c */{_IOPATH})",
        ],
    )
    def test_condelse_parses_to_cond_else(self, entry: str):
        parsed = _only_entry(_wrap_delay(entry))
        assert isinstance(parsed, Iopath)
        assert parsed.cond == CondElse()

    @pytest.mark.parametrize(
        "entry", [f"(CONDELSE {_IOPATH})", f"(COND A {_IOPATH})", _IOPATH]
    )
    def test_condition_round_trips(self, entry: str):
        assert _round_trip(_wrap_delay(entry)) == [_only_entry(_wrap_delay(entry))]

    @pytest.mark.parametrize(
        "check",
        [
            "(SETUP (CONDELSE D) (posedge CP) (1))",
            "(WIDTH (CONDX en (posedge CP)) (1))",
        ],
    )
    def test_cond_keyword_needs_a_word_boundary(self, check: str):
        with pytest.raises(SDFParseError):
            parse_sdf(_wrap_timing_check(check))


class TestCondOnBareTimingPort:
    """A bare port after a COND equation ends the equation."""

    @pytest.mark.parametrize(
        ("check", "data", "reference"),
        [
            (
                "(SETUP (COND en d) (posedge clk) (1))",
                TimingPortSpec("d", cond=Cond("en")),
                TimingPortSpec("clk", edge=EdgeType.POSEDGE),
            ),
            (
                "(HOLD (COND !en d) clk (1))",
                TimingPortSpec("d", cond=Cond("! en")),
                TimingPortSpec("clk"),
            ),
            (
                "(SETUP d (COND en == 1'b1 clk) (1))",
                TimingPortSpec("d"),
                TimingPortSpec("clk", cond=Cond("en == 1'b1")),
            ),
            (
                "(SETUP (COND (a && b) d) clk (1))",
                TimingPortSpec("d", cond=Cond("(a && b)")),
                TimingPortSpec("clk"),
            ),
            (
                "(RECOVERY (COND en 0) clk (1))",
                TimingPortSpec("0", cond=Cond("en")),
                TimingPortSpec("clk"),
            ),
        ],
    )
    def test_bare_port_after_equation(
        self, check: str, data: TimingPortSpec, reference: TimingPortSpec
    ):
        entry = _only_entry(_wrap_timing_check(check))
        assert isinstance(entry, Setup | Hold | Recovery)
        assert (entry.data, entry.reference) == (data, reference)
        assert _round_trip(_wrap_timing_check(check)) == [entry]


class TestDelayCondEquation:
    """Parentheses are kept inside an equation and unwrapped only when outermost."""

    @pytest.mark.parametrize(
        ("written", "equation"),
        [
            ("A", "A"),
            ("(A)", "A"),
            ("((A))", "(A)"),
            ("(A && (B || C))", "A && (B || C)"),
            ("(A) && (B)", "(A) && (B)"),
            ("~i1", "~ i1"),
            ("A==1'b0&&B==1'b1", "A == 1'b0 && B == 1'b1"),
        ],
    )
    def test_equation_text(self, written: str, equation: str):
        entry = _only_entry(_wrap_delay(f"(COND {written} {_IOPATH})"))
        assert isinstance(entry, Iopath)
        assert entry.cond == Cond(equation)
        assert _round_trip(_wrap_delay(f"(COND {written} {_IOPATH})")) == [entry]


class TestMixedDeltypes:
    """A DELAY holds any mix of ABSOLUTE and INCREMENT blocks, in file order."""

    @pytest.mark.parametrize(
        ("blocks", "modes"),
        [
            (
                "(ABSOLUTE {e}) (INCREMENT {e})",
                [DelayMode.ABSOLUTE, DelayMode.INCREMENT],
            ),
            (
                "(INCREMENT {e}) (ABSOLUTE {e})",
                [DelayMode.INCREMENT, DelayMode.ABSOLUTE],
            ),
            (
                "(ABSOLUTE {e}) (INCREMENT {e}) (ABSOLUTE {e})",
                [DelayMode.ABSOLUTE, DelayMode.INCREMENT, DelayMode.ABSOLUTE],
            ),
        ],
    )
    def test_modes_in_file_order(self, blocks: str, modes: list[DelayMode]):
        text = f"{_CELL_HEAD} (DELAY {blocks.format(e=_IOPATH)})))"
        entries = parse_sdf(text).cells["top"]["u1"]
        assert [e.mode for e in entries if isinstance(e, Iopath)] == modes

    def test_emitted_mixed_delay_reparses(self):
        """The writer emits ABSOLUTE and INCREMENT in one DELAY."""
        text = f"{_CELL_HEAD} (DELAY (ABSOLUTE {_IOPATH}) (INCREMENT {_IOPATH}))))"
        assert sorted(e.mode for e in _round_trip(text) if isinstance(e, Iopath)) == [
            DelayMode.ABSOLUTE,
            DelayMode.INCREMENT,
        ]


class TestNestedCondRejected:
    """A condition wraps plain entries only, so nesting is a syntax error."""

    @pytest.mark.parametrize(
        "entry",
        [
            f"(COND A (COND B {_IOPATH}))",
            f"(COND A (CONDELSE {_IOPATH}))",
            f"(CONDELSE (COND B {_IOPATH}))",
            f"(CONDELSE (CONDELSE {_IOPATH}))",
        ],
    )
    def test_nested_condition_is_a_syntax_error(self, entry: str):
        with pytest.raises(SDFParseError, match="expected one of"):
            parse_sdf(_wrap_delay(entry))


class TestNumericPortNames:
    """A port name made of digits keeps its source text."""

    @pytest.mark.parametrize("name", ["0", "1", "12", "007", "1.5"])
    def test_numeric_port_keeps_text(self, name: str):
        entry = _only_entry(_wrap_delay(f"(IOPATH {name} Z (1))"))
        assert isinstance(entry, Iopath)
        assert entry.input == PortSpec(name)


class TestTimescale:
    @pytest.mark.parametrize(
        ("written", "expected"),
        [
            ("1ns", Timescale(value=1, unit=TimeUnit.NS)),
            ("10 ps", Timescale(value=10, unit=TimeUnit.PS)),
            ("1.0 ns", Timescale(value=1, unit=TimeUnit.NS)),
            ("100us", Timescale(value=100, unit=TimeUnit.US)),
        ],
    )
    def test_valid_timescale(self, written: str, expected: Timescale):
        text = f'(DELAYFILE (SDFVERSION "3.0") (TIMESCALE {written}))'
        assert parse_sdf(text).header.timescale == expected

    @pytest.mark.parametrize("written", ["7xs", "1 nsx", "5 ns", "1e1 ns", "-1 ns"])
    def test_invalid_timescale_raises(self, written: str):
        with pytest.raises(ValueError, match="timescale"):
            parse_sdf(f'(DELAYFILE (SDFVERSION "3.0") (TIMESCALE {written}))')


@pytest.mark.parametrize(
    "call",
    [
        lambda t: t.absolute(object()),
        lambda t: t.increment(None),
        lambda t: t.cond_delay(Token("COND", "COND"), None, 1.0),
    ],
    ids=["absolute", "increment", "cond_delay"],
)
def test_unexpected_child_raises(call):
    """A child the grammar cannot produce raises instead of being skipped."""
    with pytest.raises(TypeError, match="Unexpected"):
        call(SDFTransformer())


def _callbacks(cls: type) -> list[str]:
    return sorted(
        name
        for klass in cls.__mro__
        if klass.__module__ == SDFTransformer.__module__
        for name, value in vars(klass).items()
        if callable(value) and not name.startswith("_") and name != "reset"
    )


@pytest.mark.parametrize("name", _callbacks(SDFTransformer))
def test_every_callback_names_a_grammar_symbol(name: str):
    """A callback the grammar cannot reach is dead code."""
    parser = get_parser().parser
    rules = {str(rule.origin.name) for rule in parser.rules}
    aliases = {str(rule.alias) for rule in parser.rules if rule.alias}
    terminals = {t.name for t in parser.terminals}
    assert name in rules | aliases | terminals
