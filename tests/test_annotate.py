"""Tests for Verilog SDF back-annotation."""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.model import (
    Cond,
    CondElse,
    Corner,
    DelayMode,
    DelayValues,
    Divider,
    EdgeType,
    Entry,
    Hold,
    Interconnect,
    Iopath,
    Period,
    PortSpec,
    Recovery,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    TimingPortSpec,
    Values,
    Width,
)
from sdf_toolkit.io.annotate import (
    SpecifyEntry,
    SpecifyKind,
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
from sdf_toolkit.parser.parser import parse_sdf

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


def _delay(*values: float) -> DelayValues:
    return DelayValues.of(*(Values(min=v, max=v) for v in values))


def _iopath(
    input_pin: str,
    output_pin: str,
    *values: float,
    edge: EdgeType | None = None,
    cond: Cond | CondElse | None = None,
) -> Iopath:
    return Iopath(
        input=PortSpec(input_pin, edge=edge),
        output=PortSpec(output_pin),
        mode=DelayMode.ABSOLUTE,
        delay=_delay(*values),
        cond=cond,
    )


_POS_CP = TimingPortSpec("CP", edge=EdgeType.POSEDGE)


class TestMatchSdfToModules:
    """Test matching SDF cells to Yosys modules."""

    def test_matching(self) -> None:
        sdf = SDFFile(
            header=SDFHeader(),
            cells={
                "INV": {"inst1": [_iopath("i", "z", 0.345, 0.325)]},
                "MISSING": {"inst2": [_iopath("a", "b", 1.0)]},
            },
        )
        design = YosysDesign(modules={"INV": YosysModule(name="INV")})
        matched = match_sdf_to_modules(sdf, design)
        assert "INV" in matched
        assert "MISSING" not in matched
        assert len(matched["INV"]) == 1

    def test_multiple_instances_merged(self) -> None:
        sdf = SDFFile(
            header=SDFHeader(),
            cells={
                "INV": {
                    "inst1": [_iopath("i", "z", 1.0)],
                    "inst2": [_iopath("i", "z", 2.0)],
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
        entries: list[Entry] = [_iopath("i", "z", 1.0), _iopath("i", "z", 2.0)]
        assert select_worst_case_delays(entries) == [_iopath("i", "z", 2.0)]

    @pytest.mark.parametrize(
        "entries",
        [
            [_iopath("i1", "z", 1.0), _iopath("i2", "z", 2.0)],
            [
                _iopath("CP", "Q", 1.0, edge=EdgeType.POSEDGE),
                _iopath("CP", "Q", 2.0, edge=EdgeType.NEGEDGE),
            ],
            [_iopath("i", "z", 1.0), _iopath("i", "z", 2.0, cond=Cond("EN"))],
        ],
        ids=["pins", "edges", "cond"],
    )
    def test_distinct_arcs_kept_separate(self, entries: list[Entry]) -> None:
        assert select_worst_case_delays(entries) == entries


# ── entries_to_specify ──────────────────────────────────────────────


class TestEntriesToSpecify:
    """Test conversion from SDF entries to SpecifyEntry objects."""

    @pytest.mark.parametrize(
        ("entry", "expected"),
        [
            (
                _iopath("i", "z", 0.345),
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="i",
                    to_pin="z",
                    delays=["0.345::0.345"],
                ),
            ),
            (
                _iopath("i", "z", 0.345, 0.325),
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="i",
                    to_pin="z",
                    delays=["0.345::0.345", "0.325::0.325"],
                ),
            ),
            (
                _iopath("CP", "Q", 2.0, 3.0, edge=EdgeType.POSEDGE),
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="CP",
                    to_pin="Q",
                    from_edge="posedge",
                    delays=["2::2", "3::3"],
                ),
            ),
            (
                _iopath("CP", "Q", 2.0, cond=Cond("TE == 0 && RB == 1")),
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="CP",
                    to_pin="Q",
                    delays=["2::2"],
                    condition="TE == 0 && RB == 1",
                ),
            ),
            (
                _iopath("CP", "Q", 2.0, cond=CondElse()),
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="CP",
                    to_pin="Q",
                    delays=["2::2"],
                    condition="",
                ),
            ),
            (
                Setup(
                    data=TimingPortSpec("D", cond=Cond("EN")),
                    reference=_POS_CP,
                    limit=Values.uniform(1.0),
                ),
                SpecifyEntry(
                    kind=SpecifyKind.SETUP,
                    from_pin="D",
                    to_pin="CP",
                    to_edge="posedge",
                    from_condition="EN",
                    limit="1:1:1",
                ),
            ),
            (
                Hold(
                    data=TimingPortSpec("D"),
                    reference=_POS_CP,
                    limit=Values.uniform(1.0),
                ),
                SpecifyEntry(
                    kind=SpecifyKind.HOLD,
                    from_pin="D",
                    to_pin="CP",
                    to_edge="posedge",
                    limit="1:1:1",
                ),
            ),
            (
                SetupHold(
                    data=TimingPortSpec("TI"),
                    reference=_POS_CP,
                    setup=Values.uniform(1.0),
                    hold=Values.uniform(2.0),
                ),
                SpecifyEntry(
                    kind=SpecifyKind.SETUPHOLD,
                    from_pin="TI",
                    to_pin="CP",
                    to_edge="posedge",
                    setup_limit="1:1:1",
                    hold_limit="2:2:2",
                ),
            ),
            (
                Recovery(
                    data=TimingPortSpec("RB", edge=EdgeType.POSEDGE),
                    reference=TimingPortSpec("CP", edge=EdgeType.NEGEDGE),
                    limit=Values.uniform(1.0),
                ),
                SpecifyEntry(
                    kind=SpecifyKind.RECOVERY,
                    from_pin="RB",
                    to_pin="CP",
                    from_edge="posedge",
                    to_edge="negedge",
                    limit="1:1:1",
                ),
            ),
            (
                Width(port=_POS_CP, limit=Values.uniform(1.0)),
                SpecifyEntry(
                    kind=SpecifyKind.WIDTH,
                    from_pin="CP",
                    from_edge="posedge",
                    limit="1:1:1",
                ),
            ),
            (
                Period(port=TimingPortSpec("CLK"), limit=Values.uniform(1.058)),
                SpecifyEntry(
                    kind=SpecifyKind.PERIOD, from_pin="CLK", limit="1.058:1.058:1.058"
                ),
            ),
        ],
        ids=[
            "iopath-one",
            "iopath-rise-fall",
            "iopath-edge",
            "iopath-cond",
            "iopath-condelse",
            "setup-data-cond",
            "hold",
            "setuphold",
            "recovery",
            "width",
            "period",
        ],
    )
    def test_converts(self, entry: Entry, expected: SpecifyEntry) -> None:
        assert entries_to_specify([entry]) == [expected]

    def test_interconnect_skipped(self) -> None:
        entry = Interconnect(
            source=PortSpec("a"),
            load=PortSpec("b"),
            mode=DelayMode.ABSOLUTE,
            delay=_delay(1.0),
        )
        assert entries_to_specify([entry]) == []


# ── Render specify block ────────────────────────────────────────────


class TestRenderSpecifyBlock:
    """Test rendering SpecifyEntry list to Verilog specify block text."""

    @pytest.mark.parametrize(
        ("entry", "line"),
        [
            (
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="i",
                    to_pin="z",
                    delays=["0.345::0.345", "0.325::0.325"],
                ),
                "(i => z) = (0.345::0.345, 0.325::0.325);",
            ),
            (
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="CP",
                    to_pin="Q",
                    from_edge="posedge",
                    delays=["2:2:2", "3:3:3", "4:4:4"],
                ),
                "(posedge CP => Q) = (2:2:2, 3:3:3, 4:4:4);",
            ),
            (
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="CP",
                    to_pin="Q",
                    delays=["2:2:2"],
                    condition="TE == 0",
                ),
                "if (TE == 0) (CP => Q) = (2:2:2);",
            ),
            (
                SpecifyEntry(
                    kind=SpecifyKind.IOPATH,
                    from_pin="CP",
                    to_pin="Q",
                    delays=["2:2:2"],
                    condition="",
                ),
                "ifnone (CP => Q) = (2:2:2);",
            ),
            (
                SpecifyEntry(
                    kind=SpecifyKind.SETUP,
                    from_pin="D",
                    to_pin="CP",
                    to_edge="posedge",
                    from_condition="EN",
                    limit="1:1:1",
                ),
                "$setup(D &&& EN, posedge CP, 1:1:1);",
            ),
            (
                SpecifyEntry(
                    kind=SpecifyKind.WIDTH,
                    from_pin="CP",
                    from_edge="posedge",
                    limit="1:1:1",
                ),
                "$width(posedge CP, 1:1:1);",
            ),
            (
                SpecifyEntry(
                    kind=SpecifyKind.PERIOD, from_pin="CLK", limit="1.058:1.058:1.058"
                ),
                "$period(CLK, 1.058:1.058:1.058);",
            ),
        ],
        ids=[
            "iopath",
            "iopath-three",
            "iopath-if",
            "iopath-ifnone",
            "setup-cond",
            "width",
            "period",
        ],
    )
    def test_renders(self, entry: SpecifyEntry, line: str) -> None:
        block = render_specify_block([entry])
        assert "specify" in block
        assert "endspecify" in block
        assert line in block


# Each SDF check names the data port first.  Verilog puts the reference
# first for $hold and $setuphold (IEEE 1364-2005 §15).
_CHECK_ORDER = [
    ("(SETUP D (posedge CP) (1:1:1))", "$setup(D, posedge CP, 1:1:1);"),
    ("(HOLD D (posedge CP) (2:2:2))", "$hold(posedge CP, D, 2:2:2);"),
    (
        "(SETUPHOLD D (posedge CP) (3:3:3) (4:4:4))",
        "$setuphold(posedge CP, D, 3:3:3, 4:4:4);",
    ),
    (
        "(RECOVERY (posedge RB) (posedge CP) (5:5:5))",
        "$recovery(posedge RB, posedge CP, 5:5:5);",
    ),
]


class TestTimingCheckArgumentOrder:
    """Parse an SDF check and render it, end to end."""

    @pytest.mark.parametrize(
        ("check", "line"),
        _CHECK_ORDER,
        ids=["setup", "hold", "setuphold", "recovery"],
    )
    def test_verilog_argument_order(self, check: str, line: str) -> None:
        sdf = parse_sdf(
            '(DELAYFILE (SDFVERSION "3.0") (CELL (CELLTYPE "DFF") (INSTANCE ff0)'
            f" (TIMINGCHECK {check})))"
        )
        block = render_specify_block(entries_to_specify(sdf.cells["DFF"]["ff0"]))
        assert line in block


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
        delays = [WireDelay(net_name="n1", delays=["0.1::0.1", "0.2::0.2"])]
        result = insert_wire_delays("wire n1;\n", delays)
        assert "wire #(0.1::0.1, 0.2::0.2) n1;" in result

    @pytest.mark.parametrize(
        ("verilog", "delays"),
        [
            ("wire n1;\n", [WireDelay(net_name="n2", delays=["0.1::0.1"])]),
            ("wire n1;\n", []),
            # Already annotated, so left alone.
            ("wire #(1) n1;\n", [WireDelay(net_name="n1", delays=["0.1::0.1"])]),
        ],
        ids=["no-match", "no-delays", "already-annotated"],
    )
    def test_unchanged(self, verilog: str, delays: list[WireDelay]) -> None:
        assert insert_wire_delays(verilog, delays) == verilog


# ── Resolve INTERCONNECT ────────────────────────────────────────────


def _interconnect(source: str, load: str, *values: float) -> Interconnect:
    return Interconnect(
        source=PortSpec(source),
        load=PortSpec(load),
        mode=DelayMode.ABSOLUTE,
        delay=_delay(*values),
    )


def _system_design() -> YosysDesign:
    return YosysDesign(
        modules={
            "system": YosysModule(
                name="system",
                cells={
                    "B1/C1": YosysCell(
                        name="B1/C1", cell_type="INV", connections={"i": [5]}
                    ),
                },
                netnames={"net_bc1_i": [5]},
            ),
        },
    )


class TestResolveInterconnects:
    """Test resolving INTERCONNECT entries to wire delays."""

    def test_basic_resolution(self) -> None:
        result = resolve_interconnects(
            [_interconnect("P1/z", "B1/C1/i", 0.145, 0.125)],
            _system_design(),
            top_module="system",
            sdf_divider=Divider.SLASH,
        )
        assert result == [
            WireDelay(net_name="net_bc1_i", delays=["0.145::0.145", "0.125::0.125"])
        ]

    def test_missing_top_module(self) -> None:
        result = resolve_interconnects(
            [_interconnect("a", "b/c", 1.0)],
            YosysDesign(modules={}),
            top_module="top",
            sdf_divider=Divider.SLASH,
        )
        assert result == []

    def test_more_than_three_rvalues_raises(self) -> None:
        entry = _interconnect("P1/z", "B1/C1/i", *range(1, 7))
        with pytest.raises(ValueError, match="at most 3"):
            resolve_interconnects(
                [entry],
                _system_design(),
                top_module="system",
                sdf_divider=Divider.SLASH,
            )


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
            corner=Corner.MAX,
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
