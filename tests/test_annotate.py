"""Tests for Verilog SDF back-annotation."""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.model import (
    BaseEntry,
    DelayPaths,
    EdgeType,
    EntryType,
    Hold,
    Interconnect,
    Iopath,
    Period,
    Recovery,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    Values,
    Width,
)
from sdf_toolkit.io.annotate import (
    SpecifyEntry,
    WireDelay,
    YosysCell,
    YosysDesign,
    YosysModule,
    annotate_verilog,
    build_bit_to_net_map,
    entries_to_specify,
    insert_specify_blocks,
    insert_wire_delays,
    match_sdf_to_modules,
    parse_yosys_json,
    render_specify_block,
    resolve_interconnects,
    run_yosys,
    select_worst_case_delays,
)

DATA_DIR = (Path(__file__).parent / "data").resolve()

HAS_YOSYS = shutil.which("yosys") is not None


# ── Yosys JSON parsing ──────────────────────────────────────────────


class TestParseYosysJson:
    """Test parsing Yosys JSON output into data structures."""

    def test_empty_design(self) -> None:
        design = parse_yosys_json({"modules": {}})
        assert design.modules == {}

    def test_single_module(self) -> None:
        json_data = {
            "modules": {
                "INV": {
                    "ports": {
                        "i": {"direction": "input", "bits": [2]},
                        "z": {"direction": "output", "bits": [3]},
                    },
                    "cells": {},
                    "netnames": {
                        "i": {"bits": [2]},
                        "z": {"bits": [3]},
                    },
                }
            }
        }
        design = parse_yosys_json(json_data)
        assert "INV" in design.modules
        mod = design.modules["INV"]
        assert mod.name == "INV"
        assert "i" in mod.ports
        assert mod.ports["i"].direction == "input"
        assert mod.ports["i"].bits == [2]
        assert "z" in mod.ports
        assert mod.ports["z"].direction == "output"
        assert mod.netnames["i"] == [2]
        assert mod.netnames["z"] == [3]

    def test_module_with_cells(self) -> None:
        json_data = {
            "modules": {
                "top": {
                    "ports": {},
                    "cells": {
                        "u1": {
                            "type": "INV",
                            "connections": {"i": [2], "z": [3]},
                        }
                    },
                    "netnames": {"n1": {"bits": [2]}, "n2": {"bits": [3]}},
                }
            }
        }
        design = parse_yosys_json(json_data)
        mod = design.modules["top"]
        assert "u1" in mod.cells
        assert mod.cells["u1"].cell_type == "INV"
        assert mod.cells["u1"].connections == {"i": [2], "z": [3]}


# ── Bit-to-net mapping ─────────────────────────────────────────────


class TestBuildBitToNetMap:
    """Test building reverse bit-to-net name mapping."""

    def test_simple_mapping(self) -> None:
        module = YosysModule(
            name="test",
            netnames={"a": [2], "b": [3], "c": [4, 5]},
        )
        bit_map = build_bit_to_net_map(module)
        assert bit_map[2] == "a"
        assert bit_map[3] == "b"
        assert bit_map[4] == "c"
        assert bit_map[5] == "c"

    def test_string_bits_ignored(self) -> None:
        module = YosysModule(
            name="test",
            netnames={"a": [2, "x"]},
        )
        bit_map = build_bit_to_net_map(module)
        assert 2 in bit_map
        assert "x" not in bit_map


# ── SDF-to-module matching ──────────────────────────────────────────


class TestMatchSdfToModules:
    """Test matching SDF cells to Yosys modules."""

    def test_matching(self) -> None:
        sdf = SDFFile(
            header=SDFHeader(),
            cells={
                "INV": {
                    "inst1": {
                        "iopath_i_z": Iopath(
                            name="iopath_i_z",
                            from_pin="i",
                            to_pin="z",
                            delay_paths=DelayPaths(
                                fast=Values(0.345, None, 0.345),
                                slow=Values(0.325, None, 0.325),
                            ),
                            is_absolute=True,
                        ),
                    },
                },
                "MISSING": {
                    "inst2": {
                        "iopath_a_b": Iopath(
                            name="iopath_a_b",
                            from_pin="a",
                            to_pin="b",
                            delay_paths=DelayPaths(nominal=Values(1.0, None, 1.0)),
                            is_absolute=True,
                        ),
                    },
                },
            },
        )
        design = YosysDesign(
            modules={"INV": YosysModule(name="INV")},
        )
        matched = match_sdf_to_modules(sdf, design)
        assert "INV" in matched
        assert "MISSING" not in matched
        assert len(matched["INV"]) == 1

    def test_multiple_instances_merged(self) -> None:
        entry1 = Iopath(
            name="iopath_i_z",
            from_pin="i",
            to_pin="z",
            delay_paths=DelayPaths(nominal=Values(1.0, None, 1.0)),
            is_absolute=True,
        )
        entry2 = Iopath(
            name="iopath_i_z",
            from_pin="i",
            to_pin="z",
            delay_paths=DelayPaths(nominal=Values(2.0, None, 2.0)),
            is_absolute=True,
        )
        sdf = SDFFile(
            header=SDFHeader(),
            cells={
                "INV": {
                    "inst1": {"iopath_i_z": entry1},
                    "inst2": {"iopath_i_z": entry2},
                },
            },
        )
        design = YosysDesign(modules={"INV": YosysModule(name="INV")})
        matched = match_sdf_to_modules(sdf, design)
        assert len(matched["INV"]) == 2


# ── Worst-case selection ────────────────────────────────────────────


class TestSelectWorstCaseDelays:
    """Test worst-case delay selection."""

    def test_keeps_largest(self) -> None:
        entries = [
            Iopath(
                name="a",
                from_pin="i",
                to_pin="z",
                delay_paths=DelayPaths(nominal=Values(1.0, None, 1.0)),
                is_absolute=True,
            ),
            Iopath(
                name="b",
                from_pin="i",
                to_pin="z",
                delay_paths=DelayPaths(nominal=Values(2.0, None, 2.0)),
                is_absolute=True,
            ),
        ]
        result = select_worst_case_delays(entries, "nominal", "max")
        assert len(result) == 1
        assert result[0].delay_paths.nominal.max == 2.0

    def test_different_pins_kept_separate(self) -> None:
        entries = [
            Iopath(
                name="a",
                from_pin="i1",
                to_pin="z",
                delay_paths=DelayPaths(nominal=Values(1.0, None, 1.0)),
                is_absolute=True,
            ),
            Iopath(
                name="b",
                from_pin="i2",
                to_pin="z",
                delay_paths=DelayPaths(nominal=Values(2.0, None, 2.0)),
                is_absolute=True,
            ),
        ]
        result = select_worst_case_delays(entries, "nominal", "max")
        assert len(result) == 2


# ── entries_to_specify ──────────────────────────────────────────────


class TestEntriesToSpecify:
    """Test conversion from SDF entries to SpecifyEntry objects."""

    def test_iopath_nominal(self) -> None:
        entries = [
            Iopath(
                name="iopath_i_z",
                from_pin="i",
                to_pin="z",
                delay_paths=DelayPaths(nominal=Values(0.345, None, 0.345)),
                is_absolute=True,
            ),
        ]
        result = entries_to_specify(entries)
        assert len(result) == 1
        se = result[0]
        assert se.kind == "iopath"
        assert se.from_pin == "i"
        assert se.to_pin == "z"
        assert se.rise_delay == "0.345::0.345"
        assert se.fall_delay is None

    def test_iopath_fast_slow(self) -> None:
        entries = [
            Iopath(
                name="iopath_i_z",
                from_pin="i",
                to_pin="z",
                delay_paths=DelayPaths(
                    fast=Values(0.345, None, 0.345),
                    slow=Values(0.325, None, 0.325),
                ),
                is_absolute=True,
            ),
        ]
        result = entries_to_specify(entries)
        assert len(result) == 1
        se = result[0]
        assert se.rise_delay == "0.345::0.345"
        assert se.fall_delay == "0.325::0.325"

    def test_iopath_with_edge(self) -> None:
        entries = [
            Iopath(
                name="iopath_CP_Q",
                from_pin="CP",
                to_pin="Q",
                from_pin_edge=EdgeType.POSEDGE,
                delay_paths=DelayPaths(
                    fast=Values(2.0, 2.0, 2.0),
                    slow=Values(3.0, 3.0, 3.0),
                ),
                is_absolute=True,
            ),
        ]
        result = entries_to_specify(entries)
        se = result[0]
        assert se.from_edge == "posedge"
        assert se.from_pin == "CP"

    def test_iopath_conditional(self) -> None:
        entries = [
            Iopath(
                name="iopath_i_z",
                from_pin="CP",
                to_pin="Q",
                from_pin_edge=EdgeType.POSEDGE,
                delay_paths=DelayPaths(
                    fast=Values(2.0, 2.0, 2.0),
                    slow=Values(3.0, 3.0, 3.0),
                ),
                is_absolute=True,
                is_cond=True,
                cond_equation="TE == 0 && RB == 1",
            ),
        ]
        result = entries_to_specify(entries)
        se = result[0]
        assert se.condition == "TE == 0 && RB == 1"

    def test_setup(self) -> None:
        entries = [
            Setup(
                name="setup_D_CP",
                from_pin="D",
                to_pin="CP",
                from_pin_edge=None,
                to_pin_edge=EdgeType.POSEDGE,
                delay_paths=DelayPaths(nominal=Values(1.0, 1.0, 1.0)),
                is_timing_check=True,
            ),
        ]
        result = entries_to_specify(entries)
        assert len(result) == 1
        se = result[0]
        assert se.kind == "setup"
        assert se.from_pin == "D"
        assert se.to_pin == "CP"
        assert se.to_edge == "posedge"
        assert se.rise_delay == "1:1:1"

    def test_hold(self) -> None:
        entries = [
            Hold(
                name="hold_D_CP",
                from_pin="D",
                to_pin="CP",
                from_pin_edge=None,
                to_pin_edge=EdgeType.POSEDGE,
                delay_paths=DelayPaths(nominal=Values(1.0, 1.0, 1.0)),
                is_timing_check=True,
            ),
        ]
        result = entries_to_specify(entries)
        se = result[0]
        assert se.kind == "hold"

    def test_setuphold(self) -> None:
        entries = [
            SetupHold(
                name="setuphold_TI_CP",
                from_pin="TI",
                to_pin="CP",
                from_pin_edge=None,
                to_pin_edge=EdgeType.POSEDGE,
                delay_paths=DelayPaths(
                    setup=Values(1.0, 1.0, 1.0),
                    hold=Values(2.0, 2.0, 2.0),
                ),
                is_timing_check=True,
            ),
        ]
        result = entries_to_specify(entries)
        se = result[0]
        assert se.kind == "setuphold"
        assert se.setup_limit == "1:1:1"
        assert se.hold_limit == "2:2:2"

    def test_width(self) -> None:
        entries = [
            Width(
                name="width_CP_CP",
                from_pin="CP",
                to_pin="CP",
                from_pin_edge=EdgeType.POSEDGE,
                to_pin_edge=EdgeType.POSEDGE,
                delay_paths=DelayPaths(nominal=Values(1.0, 1.0, 1.0)),
                is_timing_check=True,
            ),
        ]
        result = entries_to_specify(entries)
        se = result[0]
        assert se.kind == "width"
        assert se.from_edge == "posedge"

    def test_period(self) -> None:
        entries = [
            Period(
                name="period_CLK_CLK",
                from_pin="CLK",
                to_pin="CLK",
                delay_paths=DelayPaths(nominal=Values(1.058, 1.058, 1.058)),
                is_timing_check=True,
            ),
        ]
        result = entries_to_specify(entries)
        se = result[0]
        assert se.kind == "period"
        assert se.from_pin == "CLK"

    def test_recovery(self) -> None:
        entries = [
            Recovery(
                name="recovery_RB_CP",
                from_pin="RB",
                to_pin="CP",
                from_pin_edge=EdgeType.POSEDGE,
                to_pin_edge=EdgeType.NEGEDGE,
                delay_paths=DelayPaths(nominal=Values(1.0, 1.0, 1.0)),
                is_timing_check=True,
            ),
        ]
        result = entries_to_specify(entries)
        se = result[0]
        assert se.kind == "recovery"

    def test_no_delay_paths_skipped(self) -> None:
        entries = [
            BaseEntry(
                name="empty",
                type=EntryType.IOPATH,
                from_pin="i",
                to_pin="z",
                delay_paths=None,
            ),
        ]
        result = entries_to_specify(entries)
        assert len(result) == 0


# ── Render specify block ────────────────────────────────────────────


class TestRenderSpecifyBlock:
    """Test rendering SpecifyEntry list to Verilog specify block text."""

    def test_iopath_simple(self) -> None:
        entries = [
            SpecifyEntry(
                kind="iopath",
                from_pin="i",
                to_pin="z",
                rise_delay="0.345::0.345",
                fall_delay="0.325::0.325",
            ),
        ]
        block = render_specify_block(entries)
        assert "specify" in block
        assert "endspecify" in block
        assert "(i => z) = (0.345::0.345, 0.325::0.325);" in block

    def test_iopath_with_edge(self) -> None:
        entries = [
            SpecifyEntry(
                kind="iopath",
                from_pin="CP",
                to_pin="Q",
                from_edge="posedge",
                rise_delay="2:2:2",
                fall_delay="3:3:3",
            ),
        ]
        block = render_specify_block(entries)
        assert "(posedge CP => Q) = (2:2:2, 3:3:3);" in block

    def test_iopath_conditional(self) -> None:
        entries = [
            SpecifyEntry(
                kind="iopath",
                from_pin="CP",
                to_pin="Q",
                from_edge="posedge",
                rise_delay="2:2:2",
                fall_delay="3:3:3",
                condition="TE == 0",
            ),
        ]
        block = render_specify_block(entries)
        assert "if (TE == 0)" in block

    def test_setup_check(self) -> None:
        entries = [
            SpecifyEntry(
                kind="setup",
                from_pin="D",
                to_pin="CP",
                to_edge="posedge",
                rise_delay="1:1:1",
            ),
        ]
        block = render_specify_block(entries)
        assert "$setup(D, posedge CP, 1:1:1);" in block

    def test_hold_check(self) -> None:
        entries = [
            SpecifyEntry(
                kind="hold",
                from_pin="D",
                to_pin="CP",
                to_edge="posedge",
                rise_delay="1:1:1",
            ),
        ]
        block = render_specify_block(entries)
        assert "$hold(posedge CP, D, 1:1:1);" in block

    def test_setuphold_check(self) -> None:
        entries = [
            SpecifyEntry(
                kind="setuphold",
                from_pin="TI",
                to_pin="CP",
                to_edge="posedge",
                setup_limit="1:1:1",
                hold_limit="2:2:2",
            ),
        ]
        block = render_specify_block(entries)
        assert "$setuphold(posedge CP, TI, 1:1:1, 2:2:2);" in block

    def test_width_check(self) -> None:
        entries = [
            SpecifyEntry(
                kind="width",
                from_pin="CP",
                from_edge="posedge",
                rise_delay="1:1:1",
            ),
        ]
        block = render_specify_block(entries)
        assert "$width(posedge CP, 1:1:1);" in block

    def test_period_check(self) -> None:
        entries = [
            SpecifyEntry(
                kind="period",
                from_pin="CLK",
                rise_delay="1.058:1.058:1.058",
            ),
        ]
        block = render_specify_block(entries)
        assert "$period(CLK, 1.058:1.058:1.058);" in block

    def test_recovery_check(self) -> None:
        entries = [
            SpecifyEntry(
                kind="recovery",
                from_pin="RB",
                to_pin="CP",
                from_edge="posedge",
                to_edge="negedge",
                rise_delay="1:1:1",
            ),
        ]
        block = render_specify_block(entries)
        assert "$recovery(negedge CP, posedge RB, 1:1:1);" in block


# ── Insert specify blocks ──────────────────────────────────────────


class TestInsertSpecifyBlocks:
    """Test inserting specify blocks into Verilog text."""

    def test_single_module(self) -> None:
        verilog = "module INV(input i, output z);\n    assign z = ~i;\nendmodule\n"
        blocks = {"INV": "    specify\n        (i => z) = (1);\n    endspecify"}
        result = insert_specify_blocks(verilog, blocks)
        assert "specify" in result
        assert result.index("specify") < result.index("endmodule")

    def test_multiple_modules(self) -> None:
        verilog = (
            "module INV(input i, output z);\nendmodule\n"
            "module OR2(input i1, input i2, output z);\nendmodule\n"
        )
        blocks = {
            "INV": "    specify\n        (i => z) = (1);\n    endspecify",
            "OR2": "    specify\n        (i1 => z) = (2);\n    endspecify",
        }
        result = insert_specify_blocks(verilog, blocks)
        assert result.count("endspecify") == 2
        assert "(i => z) = (1);" in result
        assert "(i1 => z) = (2);" in result

    def test_preserves_unmatched_module(self) -> None:
        verilog = "module INV(input i, output z);\nendmodule\n"
        blocks = {"OR2": "    specify\n        (i1 => z) = (1);\n    endspecify"}
        result = insert_specify_blocks(verilog, blocks)
        assert "specify" not in result

    def test_skips_existing_specify(self) -> None:
        verilog = (
            "module INV(input i, output z);\n"
            "    specify\n"
            "        (i => z) = (0.5);\n"
            "    endspecify\n"
            "endmodule\n"
        )
        blocks = {"INV": "    specify\n        (i => z) = (1);\n    endspecify"}
        result = insert_specify_blocks(verilog, blocks)
        # Should have only 1 specify block (the original)
        assert result.count("endspecify") == 1
        assert "(0.5)" in result
        assert "(1);" not in result


# ── Insert wire delays ──────────────────────────────────────────────


class TestInsertWireDelays:
    """Test inserting wire delay annotations."""

    def test_simple_wire(self) -> None:
        verilog = "wire n1;\n"
        delays = [
            WireDelay(net_name="n1", rise_delay="0.1::0.1", fall_delay="0.2::0.2"),
        ]
        result = insert_wire_delays(verilog, delays)
        assert "wire #(0.1::0.1, 0.2::0.2) n1;" in result

    def test_no_match(self) -> None:
        verilog = "wire n1;\n"
        delays = [WireDelay(net_name="n2", rise_delay="0.1::0.1")]
        result = insert_wire_delays(verilog, delays)
        assert result == verilog

    def test_empty_delays(self) -> None:
        verilog = "wire n1;\n"
        result = insert_wire_delays(verilog, [])
        assert result == verilog

    def test_no_double_annotation(self) -> None:
        verilog = "wire #(1) n1;\n"
        delays = [WireDelay(net_name="n1", rise_delay="0.1::0.1")]
        result = insert_wire_delays(verilog, delays)
        assert result == verilog  # Already has #, should not re-annotate


# ── Resolve INTERCONNECT ────────────────────────────────────────────


class TestResolveInterconnects:
    """Test resolving INTERCONNECT entries to wire delays."""

    def test_basic_resolution(self) -> None:
        entries = [
            Interconnect(
                name="interconnect_P1/z_B1/C1/i",
                from_pin="P1/z",
                to_pin="B1/C1/i",
                delay_paths=DelayPaths(
                    fast=Values(0.145, None, 0.145),
                    slow=Values(0.125, None, 0.125),
                ),
                is_absolute=True,
            ),
        ]
        design = YosysDesign(
            modules={
                "system": YosysModule(
                    name="system",
                    cells={
                        "B1/C1": YosysCell(
                            name="B1/C1",
                            cell_type="INV",
                            connections={"i": [5]},
                        ),
                    },
                    netnames={"net_bc1_i": [5]},
                ),
            },
        )
        result = resolve_interconnects(entries, design, "system", "/")
        assert len(result) == 1
        assert result[0].net_name == "net_bc1_i"

    def test_missing_top_module(self) -> None:
        entries = [
            Interconnect(
                name="ic",
                from_pin="a",
                to_pin="b/c",
                delay_paths=DelayPaths(nominal=Values(1.0, None, 1.0)),
                is_absolute=True,
            ),
        ]
        design = YosysDesign(modules={})
        result = resolve_interconnects(entries, design, "top", "/")
        assert result == []


# ── Integration tests (require Yosys) ──────────────────────────────


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
class TestRunYosys:
    """Integration tests requiring Yosys."""

    def test_parse_test_cells(self) -> None:
        json_data = run_yosys(DATA_DIR / "test_cells.v")
        assert "modules" in json_data
        modules = json_data["modules"]
        assert "INV" in modules
        assert "OR2" in modules
        assert "AND2" in modules

    def test_inv_ports(self) -> None:
        json_data = run_yosys(DATA_DIR / "test_cells.v")
        design = parse_yosys_json(json_data)
        inv = design.modules["INV"]
        assert "i" in inv.ports
        assert "z" in inv.ports
        assert inv.ports["i"].direction == "input"
        assert inv.ports["z"].direction == "output"


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
class TestAnnotateVerilogFull:
    """End-to-end integration test."""

    def test_annotate_spec_example1(self, tmp_path: Path) -> None:
        output_path = tmp_path / "annotated.v"
        result = annotate_verilog(
            sdf_path=DATA_DIR / "spec-example1.sdf",
            verilog_path=DATA_DIR / "test_cells.v",
            output_path=output_path,
            field_name="slow",
            metric="max",
        )

        # Check that specify blocks were inserted
        assert "specify" in result
        assert "endspecify" in result

        # Check that output file was written
        assert output_path.exists()
        assert output_path.read_text() == result

    def test_annotate_stdout(self) -> None:
        result = annotate_verilog(
            sdf_path=DATA_DIR / "spec-example1.sdf",
            verilog_path=DATA_DIR / "test_cells.v",
        )
        assert "specify" in result
        # INV should have iopath i->z
        assert "(i => z)" in result


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
class TestAnnotateCli:
    """Test the CLI annotate command."""

    def test_stdout_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "annotate",
                str(DATA_DIR / "spec-example1.sdf"),
                str(DATA_DIR / "test_cells.v"),
            ],
        )
        assert result.exit_code == 0
        assert "specify" in result.stdout

    def test_file_output(self, tmp_path: Path) -> None:
        output = tmp_path / "out.v"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "annotate",
                str(DATA_DIR / "spec-example1.sdf"),
                str(DATA_DIR / "test_cells.v"),
                "-o",
                str(output),
            ],
        )
        assert result.exit_code == 0
        assert output.exists()
        content = output.read_text()
        assert "specify" in content
