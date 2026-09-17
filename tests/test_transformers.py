"""Tests for sdf_transformers.py -- transformer coverage for edge cases."""

import pytest
from conftest import DATA_DIR
from lark import Token

from sdf_toolkit.core.model import BaseEntry, EntryType, Values
from sdf_toolkit.io import sdfparse
from sdf_toolkit.parser.parser import parse_sdf
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
    """Wrap a SETUP whose ``timing_port`` COND carries ``equation``.

    The shape is the one spec example 2 uses; the equation/port_spec boundary
    here is LALR-decided, so it is copied rather than invented.
    """
    return _wrap_timing_check(f"(SETUP D (COND {equation} (posedge CP)) (1:1:1))")


def _only_entry(sdf_text: str) -> BaseEntry:
    """Return the single entry of a file built by one of the wrappers above."""
    return next(iter(parse_sdf(sdf_text).cells["top"]["u1"].values()))


def _only_entry_values(sdf_text: str) -> Values:
    """Return the nominal value triple of that single entry."""
    entry = _only_entry(sdf_text)
    assert entry.delay_paths is not None
    nominal = entry.delay_paths.nominal
    assert nominal is not None
    return nominal


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
        result = parse_sdf(sdf_content)
        cells = result.cells
        assert "XOR2" in cells
        instance = cells["XOR2"]["top.x1"]
        for entry in instance.values():
            assert entry.is_incremental is True
            assert entry.is_absolute is False


class TestCondTimingChecks:
    def test_cond_timing_checks(self):
        sdf_content = (DATA_DIR / "spec-example2.sdf").read_text()
        result = parse_sdf(sdf_content)
        cells = result.cells
        assert "CDS_GEN_FD_P_SD_RB_SB_NO" in cells
        instance = cells["CDS_GEN_FD_P_SD_RB_SB_NO"]["top.ff1"]

        setup_entries = [e for e in instance.values() if e.type == EntryType.SETUP]
        hold_entries = [e for e in instance.values() if e.type == EntryType.HOLD]
        recovery_entries = [
            e for e in instance.values() if e.type == EntryType.RECOVERY
        ]
        width_entries = [e for e in instance.values() if e.type == EntryType.WIDTH]
        setuphold_entries = [
            e for e in instance.values() if e.type == EntryType.SETUPHOLD
        ]

        assert len(setup_entries) == 1
        assert len(hold_entries) == 1
        assert len(recovery_entries) == 2
        assert len(setuphold_entries) == 1

        for entry in setup_entries + hold_entries:
            assert entry.is_cond is True
            assert entry.cond_equation is not None

        cond_widths = [w for w in width_entries if w.is_cond]
        plain_widths = [w for w in width_entries if not w.is_cond]
        assert len(cond_widths) == 2
        assert len(plain_widths) == 2

    @pytest.mark.parametrize("equation", _COND_EQUATIONS)
    def test_timing_port_cond_equation_text_preserved(self, equation: str):
        """``timing_port`` is a separate COND path and preserves text the same way."""
        entry = _only_entry(_wrap_cond_setup(equation))
        assert entry.is_cond is True
        assert entry.cond_equation == equation


class TestCondIopathCollisions:
    def test_all_conditional_iopaths_preserved(self):
        """All 4 conditional IOPATHs for CP->Q/QN must survive, not just 2."""
        sdf_content = (DATA_DIR / "spec-example2.sdf").read_text()
        result = parse_sdf(sdf_content)
        instance = result.cells["CDS_GEN_FD_P_SD_RB_SB_NO"]["top.ff1"]

        cond_iopaths = [
            e for e in instance.values() if e.type == EntryType.IOPATH and e.is_cond
        ]
        assert len(cond_iopaths) == 4

    def test_all_hold_setup_entries_preserved(self):
        """clb.sdf has multiple HOLD/SETUP CLK entries that must all survive."""
        sdf_content = (DATA_DIR / "clb.sdf").read_text()
        result = parse_sdf(sdf_content)
        instance = result.cells["LUT_OR_MEM5LRAM"]["SLICEM"]

        hold_entries = [e for e in instance.values() if e.type == EntryType.HOLD]
        setup_entries = [e for e in instance.values() if e.type == EntryType.SETUP]
        assert len(hold_entries) == 6
        assert len(setup_entries) == 6


class TestSingleFloatRvalue:
    """IEEE 1497 allows a scalar wherever a triple is, and it applies to all
    three of min:typ:max. ``rvalue`` is shared by every delay and timing check,
    so a missing scalar alternative rejects the whole file, not one entry.
    """

    @pytest.mark.parametrize(
        ("rvalue", "expected"),
        [
            ("(5)", Values(min=5.0, avg=5.0, max=5.0)),
            ("(-2.5)", Values(min=-2.5, avg=-2.5, max=-2.5)),
            ("(1e3)", Values(min=1000.0, avg=1000.0, max=1000.0)),
            ("5", Values(min=5.0, avg=5.0, max=5.0)),
            ("()", Values(min=None, avg=None, max=None)),
            ("(1.0:2.0:3.0)", Values(min=1.0, avg=2.0, max=3.0)),
        ],
    )
    def test_iopath_rvalue_forms(self, rvalue: str, expected: Values):
        """A scalar fills min, typ and max; the empty and triple forms are unchanged."""
        assert _only_entry_values(_wrap_delay(f"(IOPATH A Z {rvalue})")) == expected

    @pytest.mark.parametrize(
        ("check", "expected"),
        [
            ("(SETUP A (posedge CP) (3))", Values(min=3.0, avg=3.0, max=3.0)),
            ("(HOLD A (posedge CP) (0.5))", Values(min=0.5, avg=0.5, max=0.5)),
            ("(WIDTH (posedge CP) (2.5))", Values(min=2.5, avg=2.5, max=2.5)),
            ("(PERIOD (posedge CP) (10))", Values(min=10.0, avg=10.0, max=10.0)),
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


class TestConditionalDelays:
    def test_cond_iopath_with_equation(self):
        sdf_content = (DATA_DIR / "fixpoint.sdf").read_text()
        result = parse_sdf(sdf_content)
        cells = result.cells
        assert "routing_bel" in cells
        instance = cells["routing_bel"]["slicem/lut_c"]

        for entry in instance.values():
            assert entry.is_cond is True
            assert entry.cond_equation is not None
            assert len(entry.cond_equation) > 0

    def test_cond_increment_with_equation(self):
        sdf_content = (DATA_DIR / "spec-example3.sdf").read_text()
        result = parse_sdf(sdf_content)
        cells = result.cells
        instance = cells["XOR2"]["top.x1"]

        cond_entries = [e for e in instance.values() if e.is_cond]
        assert len(cond_entries) > 0
        for entry in cond_entries:
            assert entry.cond_equation is not None

    @pytest.mark.parametrize("equation", _COND_EQUATIONS)
    def test_cond_equation_text_preserved(self, equation: str):
        """A condition survives the parse as written.

        Scalar constants used to split, because ``SCALARCONSTANT`` has an
        optional leading digit, so ``1'b0`` lexed as a number plus ``'b0`` and
        rejoined as ``1.0 'b0``. Plain integers were converted by the ``FLOAT``
        callback, so ``TE == 0`` became ``TE == 0.0``.
        """
        entry = _only_entry(_wrap_cond_iopath(equation))
        assert entry.is_cond is True
        assert entry.cond_equation == equation

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
        assert entry.delay_paths is not None
        assert entry.delay_paths.nominal == Values(min=1.0, avg=2.0, max=3.0)


class TestPathConstraints:
    def test_pathconstraint(self):
        sdf_content = (DATA_DIR / "spec-example4.sdf").read_text()
        result = parse_sdf(sdf_content)
        cells = result.cells
        assert "XOR" in cells
        for _instance_name, instance in cells["XOR"].items():
            for entry in instance.values():
                assert entry.type == EntryType.PATHCONSTRAINT
                assert entry.is_timing_env is True
                assert entry.delay_paths is not None
                assert entry.delay_paths.rise is not None
                assert entry.delay_paths.fall is not None


class TestPortDelays:
    def test_port_delays(self):
        sdf_content = (DATA_DIR / "spec-example2.sdf").read_text()
        result = parse_sdf(sdf_content)
        cells = result.cells
        instance = cells["CDS_GEN_FD_P_SD_RB_SB_NO"]["top.ff1"]

        port_entries = [e for e in instance.values() if e.type == EntryType.PORT]
        assert len(port_entries) > 0
        for entry in port_entries:
            assert entry.from_pin == entry.to_pin


class TestDeviceDelays:
    def test_device_delays(self):
        sdf_content = (DATA_DIR / "test-device.sdf").read_text()
        result = parse_sdf(sdf_content)
        cells = result.cells
        for _celltype, instances in cells.items():
            for _inst_name, entries in instances.items():
                for entry in entries.values():
                    assert entry.type == EntryType.DEVICE
                    assert entry.from_pin == entry.to_pin


class TestEmptyRvalue:
    def test_empty_rvalue_produces_default_values(self):
        """rvalue with no args or empty token list produces Values()."""
        result = SDFTransformer().rvalue()
        assert result.min is None
        assert result.avg is None
        assert result.max is None


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

    def test_period_check_parsed(self):
        """OpenSTA-style PERIOD checks parse into Period entries."""
        result = parse_sdf(self.PERIOD_SDF)
        entries = result.cells["dff"]["ff0"]
        periods = [e for e in entries.values() if e.type == EntryType.PERIOD]
        assert len(periods) == 2

        plain = entries["period_CLK_CLK"]
        assert plain.is_timing_check
        assert plain.from_pin == "CLK"
        assert plain.to_pin == "CLK"
        assert plain.delay_paths.nominal.min == 1.058
        assert plain.delay_paths.nominal.max == 1.058

    def test_period_check_edge_qualified(self):
        """Edge-qualified PERIOD ports keep their edge."""
        result = parse_sdf(self.PERIOD_SDF)
        edged = result.cells["dff"]["ff0"]["period_CLK2_CLK2"]
        assert edged.from_pin_edge is not None
        assert edged.delay_paths.nominal.min == 2.0
        assert edged.delay_paths.nominal.max == 2.5

    def test_period_check_round_trip(self):
        """Emitting a parsed file reproduces the PERIOD check shape."""
        from sdf_toolkit.io import emit

        result = parse_sdf(self.PERIOD_SDF)
        text = emit(result, timescale="1.0 ns")
        assert "PERIOD" in text
        # Re-parse the emitted text to prove the writer output is valid.
        reparsed = parse_sdf(text)
        entries = reparsed.cells["dff"]["ff0"]
        periods = [e for e in entries.values() if e.type == EntryType.PERIOD]
        assert len(periods) == 2
