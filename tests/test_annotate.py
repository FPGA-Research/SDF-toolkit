"""Tests for Verilog SDF back-annotation."""

import math
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.model import (
    Cond,
    CondElse,
    DelayMode,
    DelayValues,
    Divider,
    EdgeType,
    Entry,
    Iopath,
    PortSpec,
    SDFFile,
    Setup,
    TimingPortSpec,
    Values,
)
from sdf_toolkit.io.annotate import (
    AnnotationError,
    PortDirection,
    SdfPin,
    SourceSpan,
    YosysDesign,
    YosysError,
    YosysJson,
    YosysModule,
    YosysPort,
    annotate_verilog,
    entries_to_specify,
    format_specify_item,
    match_sdf_to_modules,
    parse_yosys_json,
    run_yosys,
    select_worst_case_delays,
    verilog_identifier,
)
from sdf_toolkit.parser.parser import parse_sdf

DATA_DIR = (Path(__file__).parent / "data").resolve()

requires_yosys = pytest.mark.skipif(
    shutil.which("yosys") is None, reason="Yosys not installed"
)
requires_iverilog = pytest.mark.skipif(
    shutil.which("iverilog") is None, reason="iverilog not installed"
)


def _sdf(cells: str, *, header: str = '(DESIGN "top") (DIVIDER /)') -> str:
    return f'(DELAYFILE (SDFVERSION "3.0") {header} {cells})'


def _cell(cell_type: str, body: str, *, instance: str = "") -> str:
    return f'(CELL (CELLTYPE "{cell_type}") (INSTANCE {instance}) {body})'


def _annotate(tmp_path: Path, *, verilog: str, sdf: str) -> str:
    (tmp_path / "in.v").write_text(verilog)
    (tmp_path / "in.sdf").write_text(sdf)
    return annotate_verilog(
        sdf_path=tmp_path / "in.sdf", verilog_path=tmp_path / "in.v"
    )


def _iverilog(tmp_path: Path, text: str, *, generation: str) -> None:
    out = tmp_path / "out.v"
    out.write_text(text)
    result = subprocess.run(
        ["iverilog", f"-g{generation}", "-o", str(tmp_path / "a.out"), str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


# ── Yosys ───────────────────────────────────────────────────────────


class TestRunYosys:
    """The Yosys helper fails loudly when Yosys is missing or fails."""

    @pytest.mark.parametrize(
        ("script", "match"),
        [
            (None, "Yosys is required"),
            ("echo parse trouble >&2\nexit 3", "exit 3"),
        ],
        ids=["missing", "non-zero-exit"],
    )
    def test_fails_loudly(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        script: str | None,
        match: str,
    ) -> None:
        if script is not None:
            fake = tmp_path / "yosys"
            fake.write_text(f"#!/bin/sh\n{script}\n")
            fake.chmod(0o755)
        monkeypatch.setenv("PATH", str(tmp_path))
        with pytest.raises(YosysError, match=match) as info:
            run_yosys(DATA_DIR / "test_cells.v")
        if script is not None:
            assert "parse trouble" in str(info.value)

    @requires_yosys
    def test_reads_ports(self) -> None:
        design = parse_yosys_json(run_yosys(DATA_DIR / "test_cells.v"))
        assert set(design.modules) == {"INV", "OR2", "AND2"}
        assert design.modules["INV"].ports["i"].direction == PortDirection.INPUT
        assert design.modules["INV"].ports["z"].direction == PortDirection.OUTPUT
        assert design.modules["INV"].src is not None


def _json() -> YosysJson:
    return {
        "creator": "test",
        "modules": {
            "top": {
                "attributes": {"src": "/x/a.v:1.1-9.10"},
                "ports": {
                    "a": {"direction": "input", "bits": [2, 3], "offset": 4, "upto": 1}
                },
                "cells": {
                    "u1": {
                        "hide_name": 0,
                        "type": "INV",
                        "parameters": {},
                        "attributes": {"src": "/x/a.v:3.3-3.20"},
                        "connections": {"i": [2], "z": ["0"]},
                    }
                },
                "netnames": {
                    "a": {"hide_name": 0, "bits": [2, 3], "attributes": {}},
                    "$n": {"hide_name": 1, "bits": [5], "attributes": {}},
                },
            }
        },
    }


class TestParseYosysJson:
    """Every field comes from the JSON; a missing one raises."""

    def test_reads_fields(self) -> None:
        top = parse_yosys_json(_json()).modules["top"]
        assert top.src == SourceSpan(Path("/x/a.v"), 1, 1, 9, 10)
        assert top.ports["a"] == YosysPort(
            "a", PortDirection.INPUT, [2, 3], offset=4, upto=True
        )
        assert top.cells["u1"].cell_type == "INV"
        assert top.cells["u1"].connections == {"i": [2], "z": ["0"]}
        assert top.netnames["$n"].hidden
        assert top.netnames["a"].src is None

    @pytest.mark.parametrize(
        ("table", "name", "key"),
        [
            ("ports", "a", "direction"),
            ("cells", "u1", "type"),
            ("netnames", "a", "bits"),
        ],
    )
    def test_missing_field_raises(self, table: str, name: str, key: str) -> None:
        data = cast("dict[str, Any]", _json())
        del data["modules"]["top"][table][name][key]
        with pytest.raises(KeyError, match=key):
            parse_yosys_json(cast("YosysJson", data))

    @pytest.mark.parametrize("text", ["a.v:1.1-2.3|b.v:1.1-2.3", "a.v:1.1", "nofile"])
    def test_bad_src_raises(self, text: str) -> None:
        with pytest.raises(AnnotationError, match="not one"):
            SourceSpan.parse(text)

    @pytest.mark.parametrize(
        ("offset", "upto", "index", "position"),
        [(0, False, 0, 0), (0, False, 3, 3), (4, False, 5, 1), (0, True, 0, 3)],
    )
    def test_bit_position_and_index(
        self, offset: int, upto: bool, index: int, position: int
    ) -> None:
        port = YosysPort("a", PortDirection.INPUT, [1, 2, 3, 4], offset, upto)
        assert port.bit_position(index) == position
        assert port.bit_index(position) == index

    def test_bit_outside_port_raises(self) -> None:
        port = YosysPort("a", PortDirection.INPUT, [1, 2])
        with pytest.raises(AnnotationError, match="no bit 2"):
            port.bit_position(2)


# ── SDF names ───────────────────────────────────────────────────────


class TestSdfPin:
    """SDF paths split on unescaped dividers; escapes become literal."""

    @pytest.mark.parametrize(
        ("text", "divider", "pin"),
        [
            ("A", Divider.SLASH, SdfPin((), "A", None)),
            ("u1/u2/A", Divider.SLASH, SdfPin(("u1", "u2"), "A", None)),
            ("u1.A", Divider.DOT, SdfPin(("u1",), "A", None)),
            ("u1.A", Divider.SLASH, SdfPin((), "u1.A", None)),
            ("A[3]", Divider.SLASH, SdfPin((), "A", 3)),
            (r"u\/2/A[0]", Divider.SLASH, SdfPin(("u/2",), "A", 0)),
            (r"a\[1\]", Divider.SLASH, SdfPin((), "a[1]", None)),
            (r"u[0]/A", Divider.SLASH, SdfPin(("u[0]",), "A", None)),
        ],
    )
    def test_parse(self, text: str, divider: Divider, pin: SdfPin) -> None:
        assert SdfPin.parse(text, divider) == pin

    @pytest.mark.parametrize(
        ("text", "match"),
        [("u1//A", "empty segment"), ("A[1:0]", "one bit"), ("A\\", "lone escape")],
    )
    def test_bad_path_raises(self, text: str, match: str) -> None:
        with pytest.raises(AnnotationError, match=match):
            SdfPin.parse(text, Divider.SLASH)

    @pytest.mark.parametrize(
        ("name", "identifier"),
        [("a_1$", "a_1$"), ("a[1]", "\\a[1] "), ("1a", "\\1a "), ("u/2", "\\u/2 ")],
    )
    def test_verilog_identifier(self, name: str, identifier: str) -> None:
        assert verilog_identifier(name) == identifier


# ── Matching and worst-case selection ───────────────────────────────


def _delay(*values: float) -> DelayValues:
    return DelayValues.of(*(Values.uniform(v) for v in values))


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


class TestMatchSdfToModules:
    """Each SDF cell type needs a module."""

    def test_collects_every_instance(self) -> None:
        sdf = SDFFile(
            cells={"INV": {"a": [_iopath("i", "z", 1)], "b": [_iopath("i", "z", 2)]}}
        )
        design = YosysDesign(modules={"INV": YosysModule(name="INV")})
        assert match_sdf_to_modules(sdf, design) == {
            "INV": [_iopath("i", "z", 1), _iopath("i", "z", 2)]
        }

    def test_missing_module_raises(self) -> None:
        sdf = SDFFile(cells={"MISSING": {"u": [_iopath("a", "b", 1)]}})
        with pytest.raises(AnnotationError, match=r"\['MISSING'\] have no module"):
            match_sdf_to_modules(sdf, YosysDesign())


class TestSelectWorstCaseDelays:
    """One entry per arc, the largest at the corner."""

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


# ── Specify items ───────────────────────────────────────────────────


def _cell_module() -> YosysModule:
    inputs = {"i": 1, "CP": 1, "D": 1, "RB": 1, "TE": 1, "EN": 1, "A": 4, "a[1]": 1}
    outputs = {"z": 1, "Q": 1, "Z": 1}
    ports = {
        name: YosysPort(name, direction, list(range(width)))
        for table, direction in (
            (inputs, PortDirection.INPUT),
            (outputs, PortDirection.OUTPUT),
        )
        for name, width in table.items()
    }
    return YosysModule(name="CELL", ports=ports)


def _lines(entries: list[Entry]) -> list[str]:
    items = entries_to_specify(entries, _cell_module(), divider=Divider.SLASH)
    return [format_specify_item(item) for item in items]


def _parsed(body: str) -> list[Entry]:
    return parse_sdf(_sdf(_cell("CELL", body))).cells["CELL"][""]


_POS_CP = TimingPortSpec("CP", edge=EdgeType.POSEDGE)


class TestSpecifyItems:
    """Each entry becomes one IEEE 1364-2005 specify statement."""

    @pytest.mark.parametrize(
        ("body", "line"),
        [
            (
                "(DELAY (ABSOLUTE (IOPATH i z (1:2:3) (0.5:1:1.5))))",
                "(i => z) = (1:2:3, 0.5:1:1.5);",
            ),
            (
                "(DELAY (ABSOLUTE (IOPATH (posedge CP) Q (2))))",
                "(posedge CP => (Q : 1'bx)) = (2:2:2);",
            ),
            (
                "(DELAY (ABSOLUTE (IOPATH (negedge CP) Q (2) (3))))",
                "(negedge CP => (Q : 1'bx)) = (2:2:2, 3:3:3);",
            ),
            (
                "(DELAY (ABSOLUTE (COND TE==1'b0 (IOPATH CP Q (2)))))",
                "if (TE == 1'b0) (CP => Q) = (2:2:2);",
            ),
            (
                "(DELAY (ABSOLUTE (IOPATH A[1] Z (1e-05))))",
                "(A[1] => Z) = (1e-05:1e-05:1e-05);",
            ),
            (r"(DELAY (ABSOLUTE (IOPATH a\[1\] Z (1))))", "(\\a[1]  => Z) = (1:1:1);"),
            ("(TIMINGCHECK (WIDTH (posedge CP) (1)))", "$width(posedge CP, (1:1:1));"),
            (
                "(TIMINGCHECK (PERIOD (negedge CP) (2)))",
                "$period(negedge CP, (2:2:2));",
            ),
        ],
        ids=[
            "iopath",
            "posedge",
            "negedge",
            "cond",
            "bus-bit",
            "escaped",
            "width",
            "period",
        ],
    )
    def test_formats(self, body: str, line: str) -> None:
        assert _lines(_parsed(body)) == [line]

    @pytest.mark.parametrize(
        ("entry", "line"),
        [
            (_iopath("CP", "Q", 2, cond=CondElse()), "ifnone (CP => Q) = (2:2:2);"),
            (
                Setup(
                    data=TimingPortSpec("D", cond=Cond("EN")),
                    reference=_POS_CP,
                    limit=Values.uniform(1),
                ),
                "$setup(D &&& EN, posedge CP, (1:1:1));",
            ),
        ],
        ids=["ifnone", "check-cond"],
    )
    def test_formats_built(self, entry: Entry, line: str) -> None:
        assert _lines([entry]) == [line]

    # Each SDF check names the data port first.  Verilog puts the reference
    # first for $hold and $setuphold; $recovery and $removal keep SDF order.
    @pytest.mark.parametrize(
        ("check", "line"),
        [
            ("(SETUP D (posedge CP) (1:2:3))", "$setup(D, posedge CP, (1:2:3));"),
            ("(HOLD D (posedge CP) (2))", "$hold(posedge CP, D, (2:2:2));"),
            (
                "(SETUPHOLD D (posedge CP) (3) (-1))",
                "$setuphold(posedge CP, D, (3:3:3), (-1:-1:-1));",
            ),
            (
                "(RECOVERY (posedge RB) (posedge CP) (5))",
                "$recovery(posedge RB, posedge CP, (5:5:5));",
            ),
            (
                "(REMOVAL (negedge RB) (posedge CP) (6))",
                "$removal(negedge RB, posedge CP, (6:6:6));",
            ),
        ],
        ids=["setup", "hold", "setuphold", "recovery", "removal"],
    )
    def test_check_argument_order(self, check: str, line: str) -> None:
        assert _lines(_parsed(f"(TIMINGCHECK {check})")) == [line]

    @pytest.mark.parametrize(
        ("body", "match"),
        [
            (
                "(DELAY (ABSOLUTE (IOPATH i z (::2))))",
                r"IOPATH i z rvalue 0: \(::2\) lacks a corner",
            ),
            (
                "(DELAY (ABSOLUTE (IOPATH i z (1:2:3) ())))",
                "IOPATH i z rvalue 1: an empty",
            ),
            (
                "(TIMINGCHECK (SETUP D (posedge CP) (1::3)))",
                r"SETUP D \(posedge CP\): \(1::3\)",
            ),
            ("(DELAY (ABSOLUTE (PORT i (1))))", "PORT i: a PORT delay"),
            ("(DELAY (ABSOLUTE (DEVICE Q (1))))", "DEVICE Q: DEVICE"),
            (
                "(DELAY (ABSOLUTE (INTERCONNECT i z (1))))",
                "INTERCONNECT i z: an INTERCONNECT",
            ),
            (
                "(TIMINGENV (PATHCONSTRAINT i z (1) (1)))",
                "PATHCONSTRAINT i z: PATHCONSTRAINT",
            ),
            ("(DELAY (ABSOLUTE (IOPATH x z (1))))", "has no port x"),
            ("(DELAY (ABSOLUTE (IOPATH z i (1))))", "source z is an output"),
            (
                "(DELAY (ABSOLUTE (IOPATH i (posedge z) (1))))",
                "destination takes no edge",
            ),
            ("(DELAY (ABSOLUTE (IOPATH A z (1))))", "joins 4 bits to 1"),
            ("(DELAY (ABSOLUTE (IOPATH A[7] Z (1))))", "no bit 7"),
            ("(DELAY (ABSOLUTE (IOPATH u/i z (1))))", "hierarchical"),
            ("(TIMINGCHECK (WIDTH CP (1)))", r"\$width needs an edge"),
            ("(DELAY (INCREMENT (IOPATH i z (1))))", "INCREMENT IOPATH i z: INCREMENT"),
            ("(DELAY (ABSOLUTE (IOPATH (01 i) z (1))))", "edge 01 has no posedge"),
            ("(TIMINGCHECK (WIDTH (10 CP) (1)))", "edge 10 has no posedge"),
            ("(DELAY (ABSOLUTE (IOPATH i z ((1) (0.5)))))", "pulse limits"),
            ("(DELAY (ABSOLUTE (IOPATH i z (RETAIN (1)) (2))))", "RETAIN has no"),
            ("(DELAY (ABSOLUTE (NETDELAY z (1))))", "NETDELAY z: a NETDELAY"),
            ("(DELAY (PATHPULSE i z (1)))", r"PATHPULSE i z: PATHPULSE would"),
            ("(DELAY (PATHPULSEPERCENT (1)))", "PATHPULSEPERCENT would"),
            (
                "(TIMINGCHECK (SETUPHOLD i (posedge CP) (1) (2) (SCOND i)))",
                "SCOND and CCOND would be",
            ),
            (
                "(TIMINGCHECK (RECREM i (posedge CP) (1) (2)))",
                r"emit \$recrem",
            ),
            ("(TIMINGCHECK (SKEW (posedge CP) i (1)))", r"emit \$skew"),
            ("(TIMINGCHECK (NOCHANGE (posedge CP) i (1) (2)))", r"emit \$nochange"),
            ("(TIMINGENV (SLACK i (1) (2) (3) (4)))", "SLACK has no Verilog form"),
        ],
        ids=[
            "missing-corner",
            "empty-rvalue",
            "missing-corner-limit",
            "port",
            "device",
            "interconnect",
            "pathconstraint",
            "unknown-port",
            "reversed-path",
            "edge-on-destination",
            "width-mismatch",
            "bit-out-of-range",
            "hierarchical-port",
            "width-without-edge",
            "increment",
            "transition-edge-path",
            "transition-edge-check",
            "pulse-limits",
            "retain",
            "netdelay",
            "pathpulse",
            "pathpulsepercent",
            "setuphold-scond",
            "recrem",
            "skew",
            "nochange",
            "slack",
        ],
    )
    def test_unexpressible_entry_raises(self, body: str, match: str) -> None:
        with pytest.raises(AnnotationError, match=f"CELL: .*{match}"):
            _lines(_parsed(body))

    @pytest.mark.parametrize(
        ("entry", "match"),
        [
            (
                _iopath("CP", "Q", 1, edge=EdgeType.POSEDGE, cond=CondElse()),
                "ifnone only on a path without an edge",
            ),
            (_iopath("i", "z", math.inf), "not finite"),
        ],
        ids=["ifnone-edge", "inf"],
    )
    def test_unexpressible_built_entry_raises(self, entry: Entry, match: str) -> None:
        with pytest.raises(AnnotationError, match=match):
            _lines([entry])


# ── Specify block insertion (Yosys spans) ───────────────────────────

_BLOCK = "    specify\n        (a => y) = (1:2:3);\n    endspecify\n"
_IOPATH = "(DELAY (ABSOLUTE (IOPATH a y (1:2:3))))"


@requires_yosys
@requires_iverilog
class TestSpecifyInsertion:
    """The block lands before the endmodule Yosys places for the module."""

    @pytest.mark.parametrize(
        ("cell_type", "before", "after", "generation"),
        [
            (
                "X",
                "(* blackbox *) module X(input a, output y);\nendmodule\n",
                f"(* blackbox *) module X(input a, output y);\n{_BLOCK}endmodule\n",
                "2005",
            ),
            (
                "X",
                "module\n  X (input a, output y);\n  assign y = a;\nendmodule // X\n",
                "module\n  X (input a, output y);\n"
                f"  assign y = a;\n{_BLOCK}endmodule // X\n",
                "2005",
            ),
            (
                "X$1",
                "module \\X$1 (input a, output y);\n  assign y = a;\nendmodule\n",
                "module \\X$1 (input a, output y);\n"
                f"  assign y = a;\n{_BLOCK}endmodule\n",
                "2005",
            ),
            (
                "X",
                "module X #(\n  parameter W = 1\n) (input a, output y);\n"
                "  assign y = a;\nendmodule : X\n",
                "module X #(\n  parameter W = 1\n) (input a, output y);\n"
                f"  assign y = a;\n{_BLOCK}endmodule : X\n",
                "2005-sv",
            ),
            (
                "X",
                "module Y(input a); // specify\nendmodule\n"
                "module X(input a, output y); assign y = a; endmodule\n",
                "module Y(input a); // specify\nendmodule\n"
                f"module X(input a, output y); assign y = a; \n{_BLOCK}endmodule\n",
                "2005",
            ),
        ],
        ids=["blackbox", "name-on-next-line", "escaped-name", "label", "one-line"],
    )
    def test_inserts(
        self, tmp_path: Path, cell_type: str, before: str, after: str, generation: str
    ) -> None:
        result = _annotate(
            tmp_path, verilog=before, sdf=_sdf(_cell(cell_type, _IOPATH))
        )
        assert result == after
        _iverilog(tmp_path, result, generation=generation)

    def test_existing_specify_raises(self, tmp_path: Path) -> None:
        verilog = (
            "module X(input a, output y);\n  assign y = a;\n"
            "  specify // x\n    (a => y) = 1;\n  endspecify\nendmodule\n"
        )
        with pytest.raises(AnnotationError, match="X already has a specify block"):
            _annotate(tmp_path, verilog=verilog, sdf=_sdf(_cell("X", _IOPATH)))


# ── Net delays ──────────────────────────────────────────────────────

_NETLIST = """\
module BUF(input i, output z);
  assign z = i;
endmodule

module AND2(input [1:0] A, output Z);
  assign Z = &A;
endmodule

module sub(input i, output z);
  wire m;
  wire n1;
  BUF ub (.i(i), .z(m));
  BUF uc (.i(m), .z(z));
endmodule

module top(input a, output y, output y2);
  wire n1;
  wire n10;
  wire n2, n4;
  wire n3;
  wire \\n$5 ;
  wire [1:0] bus;
  BUF u0 (.i(a), .z(n1));
  BUF u1 (.i(n1), .z(n10));
  AND2 u2 (.A({n10, n1}), .Z(n2));
  sub u3 (.i(n2), .z(n3));
  BUF \\u4/x (.i(n3), .z(y));
  BUF u5 (.i(bus[0]), .z(y2));
  BUF u6 (.i(1'b0), .z());
  BUF u7 (.i(\\n$5 ), .z());
endmodule
"""


def _interconnects(*loads: tuple[str, str], instance: str = "") -> str:
    body = " ".join(f"(INTERCONNECT u0/z {load} {delay})" for load, delay in loads)
    return _sdf(_cell("top", f"(DELAY (ABSOLUTE {body}))", instance=instance))


@requires_yosys
class TestNetDelays:
    """Each INTERCONNECT lands on the one net that drives its load."""

    @pytest.mark.parametrize(
        ("load", "declaration"),
        [
            ("u1/i", "  wire #(1:2:3) n1;\n  wire n10;\n"),
            ("u2/A[1]", "  wire n1;\n  wire #(1:2:3) n10;\n"),
            ("u2/A[0]", "  wire #(1:2:3) n1;\n  wire n10;\n"),
            ("u3/uc/i", "  wire #(1:2:3) m;\n  wire n1;\n"),
            (r"u4\/x/i", "  wire n2, n4;\n  wire #(1:2:3) n3;\n"),
            ("u7/i", "  wire #(1:2:3) \\n$5 ;\n"),
        ],
        ids=[
            "n1-not-n10",
            "bus-pin",
            "bus-pin-low",
            "nested",
            "escaped-divider",
            "escaped-net",
        ],
    )
    def test_lands_on_one_net(
        self, tmp_path: Path, load: str, declaration: str
    ) -> None:
        result = _annotate(
            tmp_path, verilog=_NETLIST, sdf=_interconnects((load, "(1:2:3)"))
        )
        assert declaration in result
        assert result.count("#(") == 1
        assert result.replace("#(1:2:3) ", "") == _NETLIST
        out = tmp_path / "out.v"
        out.write_text(result)
        parse_yosys_json(run_yosys(out))

    def test_cell_instance_prefixes_load(self, tmp_path: Path) -> None:
        body = "(DELAY (ABSOLUTE (INTERCONNECT ub/z uc/i (4))))"
        sdf = _sdf(_cell("sub", body, instance="u3"))
        result = _annotate(tmp_path, verilog=_NETLIST, sdf=sdf)
        assert "  wire #(4:4:4) m;\n" in result

    def test_header_divider(self, tmp_path: Path) -> None:
        body = "(DELAY (ABSOLUTE (INTERCONNECT u0.z u3.uc.i (4))))"
        sdf = _sdf(_cell("top", body), header='(DESIGN "top") (DIVIDER .)')
        result = _annotate(tmp_path, verilog=_NETLIST, sdf=sdf)
        assert "  wire #(4:4:4) m;\n" in result

    def test_equal_delays_share_a_net(self, tmp_path: Path) -> None:
        sdf = _interconnects(("u1/i", "(1) (2)"), ("u2/A[0]", "(1) (2)"))
        result = _annotate(tmp_path, verilog=_NETLIST, sdf=sdf)
        assert "  wire #(1:1:1, 2:2:2) n1;\n" in result

    @pytest.mark.parametrize(
        ("sdf", "match"),
        [
            (
                _interconnects(("u1/i", "(1)"), ("u2/A[0]", "(2)")),
                r"INTERCONNECT u0/z u2/A\[0\]: net top.n1 already takes",
            ),
            (_interconnects(("u3/i", "(1)")), "expected a declaration"),
            (_interconnects(("u3/ub/i", "(1)")), "net i is a port of sub"),
            (_interconnects(("u0/i", "(1)")), "net a is a port of top"),
            (_interconnects(("y", "(1)")), "load y is a port of top"),
            (_interconnects(("u2/A", "(1)")), "has 2 bits; select one"),
            (_interconnects(("u5/i", "(1)")), "net bus is a vector"),
            (_interconnects(("u6/i", "(1)")), "tied to constant '0'"),
            (_interconnects(("u9/i", "(1)")), "u9 matches 0 cells"),
            (_interconnects(("u1/q", "(1)")), "has no pin q"),
            (_interconnects(("u1/i", "(1) (2) (3) (4) (5) (6)")), "at most 3"),
            (_interconnects(("u1/i", "(::1)")), "lacks a corner"),
            (_interconnects(("u1/i", "(1)"), instance="*"), r"INSTANCE \*"),
            (
                _sdf(
                    _cell("top", "(DELAY (ABSOLUTE (INTERCONNECT a u1/i (1))))"),
                    header="",
                ),
                "DESIGN None",
            ),
            (
                _sdf(_cell("top", "(DELAY (INCREMENT (INTERCONNECT u0/z u1/i (1))))")),
                "INCREMENT INTERCONNECT u0/z u1/i: INCREMENT",
            ),
        ],
        ids=[
            "conflict",
            "shared-declaration",
            "port-of-submodule",
            "top-port-net",
            "top-port-load",
            "vector-pin",
            "vector-net",
            "constant",
            "unknown-instance",
            "unknown-pin",
            "six-rvalues",
            "missing-corner",
            "wildcard",
            "no-design",
            "increment",
        ],
    )
    def test_raises(self, tmp_path: Path, sdf: str, match: str) -> None:
        with pytest.raises(AnnotationError, match=match):
            _annotate(tmp_path, verilog=_NETLIST, sdf=sdf)

    def test_module_instantiated_twice_raises(self, tmp_path: Path) -> None:
        netlist = _NETLIST.replace("  sub u3", "  sub u8 (.i(a), .z());\n  sub u3")
        with pytest.raises(AnnotationError, match="instantiated 2 times"):
            _annotate(tmp_path, verilog=netlist, sdf=_interconnects(("u3/uc/i", "(1)")))


# ── End to end ──────────────────────────────────────────────────────

_LIBRARY = """\
module INV(input i, output z);
  assign z = ~i;
endmodule

module DFF(input D, input CP, input RB, output reg Q);
  always @(posedge CP or negedge RB)
    if (!RB) Q <= 1'b0;
    else Q <= D;
endmodule

module top(input a, input clk, input rst, output q);
  wire n1;
  INV u0 (.i(a), .z(n1));
  DFF f0 (.D(n1), .CP(clk), .RB(rst), .Q(q));
endmodule
"""

_LIBRARY_SDF = _sdf(
    _cell("INV", "(DELAY (ABSOLUTE (IOPATH i z (1:2:3) (2:3:4))))", instance="u0")
    + _cell(
        "DFF",
        "(DELAY (ABSOLUTE (IOPATH (posedge CP) Q (1) (2)) (IOPATH (negedge RB) Q (3))))"
        " (TIMINGCHECK (SETUP D (posedge CP) (0.5)) (HOLD D (posedge CP) (0.25))"
        " (SETUPHOLD D (negedge CP) (0.5) (0.25))"
        " (RECOVERY (posedge RB) (posedge CP) (1))"
        " (REMOVAL (posedge RB) (posedge CP) (2)) (WIDTH (posedge CP) (3))"
        " (PERIOD (posedge CP) (6)))",
        instance="f0",
    )
)

_DFF_BLOCK = """\
    specify
        (posedge CP => (Q : 1'bx)) = (1:1:1, 2:2:2);
        (negedge RB => (Q : 1'bx)) = (3:3:3);
        $setup(D, posedge CP, (0.5:0.5:0.5));
        $hold(posedge CP, D, (0.25:0.25:0.25));
        $setuphold(negedge CP, D, (0.5:0.5:0.5), (0.25:0.25:0.25));
        $recovery(posedge RB, posedge CP, (1:1:1));
        $removal(posedge RB, posedge CP, (2:2:2));
        $width(posedge CP, (3:3:3));
        $period(posedge CP, (6:6:6));
    endspecify
"""


@requires_yosys
@requires_iverilog
class TestAnnotateEndToEnd:
    """Annotate a small library and netlist, then compile it."""

    def test_compiles_with_iverilog(self, tmp_path: Path) -> None:
        result = _annotate(tmp_path, verilog=_LIBRARY, sdf=_LIBRARY_SDF)
        assert f"    else Q <= D;\n{_DFF_BLOCK}endmodule" in result
        assert "        (i => z) = (1:2:3, 2:3:4);\n" in result
        _iverilog(tmp_path, result, generation="2005")

    def test_writes_output(self, tmp_path: Path) -> None:
        (tmp_path / "in.v").write_text(_LIBRARY)
        (tmp_path / "in.sdf").write_text(_LIBRARY_SDF)
        output = tmp_path / "annotated.v"
        result = annotate_verilog(
            sdf_path=tmp_path / "in.sdf",
            verilog_path=tmp_path / "in.v",
            output_path=output,
        )
        assert output.read_text() == result

    def test_unmatched_cell_type_raises(self) -> None:
        # The "system" cell holds the INTERCONNECTs; test_cells.v has no such module.
        with pytest.raises(AnnotationError, match=r"\['system'\] have no module"):
            annotate_verilog(
                sdf_path=DATA_DIR / "spec-example1.sdf",
                verilog_path=DATA_DIR / "test_cells.v",
            )

    @pytest.mark.parametrize("to_file", [False, True], ids=["stdout", "file"])
    def test_cli(self, tmp_path: Path, to_file: bool) -> None:
        (tmp_path / "in.v").write_text(_LIBRARY)
        (tmp_path / "in.sdf").write_text(_LIBRARY_SDF)
        output = tmp_path / "out.v"
        args = ["annotate", str(tmp_path / "in.sdf"), str(tmp_path / "in.v")]
        result = CliRunner().invoke(
            app, args + (["-o", str(output)] if to_file else [])
        )
        assert result.exit_code == 0, result.output
        text = output.read_text() if to_file else result.stdout
        assert _DFF_BLOCK in text


@requires_yosys
def test_cli_unmatched_cell_type_reports_error() -> None:
    # The README example: the Verilog must define the top-level DESIGN cell too.
    args = ["annotate", str(DATA_DIR / "spec-example1.sdf")]
    result = CliRunner().invoke(app, [*args, str(DATA_DIR / "test_cells.v")])
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: SDF cell types ['system'] have no module")
