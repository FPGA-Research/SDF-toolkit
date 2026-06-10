"""Tests for sdf_transformers.py -- transformer coverage for edge cases."""

import pytest
from conftest import DATA_DIR
from lark import Token

from sdf_toolkit.core.model import EntryType
from sdf_toolkit.parser.parser import parse_sdf
from sdf_toolkit.parser.transformers import SDFTransformer


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
    def test_single_float_value(self):
        sdf_content = (DATA_DIR / "spec-example1.sdf").read_text()
        result = parse_sdf(sdf_content)
        assert len(result.cells) > 0


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
