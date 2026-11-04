"""Transport-buffer annotation: delay reading, rewiring and pulse survival."""

import shutil
import subprocess
from pathlib import Path
from typing import cast

import pytest
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.model import Corner, EdgeSelect
from sdf_toolkit.io.annotate import AnnotationError, YosysModuleJson
from sdf_toolkit.io.transport import (
    RoutedDelays,
    annotate_transport,
    insert_transport_buffers,
    routed_delays,
)
from sdf_toolkit.parser.parser import parse_sdf

requires_yosys = pytest.mark.skipif(
    shutil.which("yosys") is None, reason="Yosys not installed"
)
requires_iverilog = pytest.mark.skipif(
    shutil.which("iverilog") is None, reason="Icarus Verilog not installed"
)


def _sdf(body: str, *, timescale: str = "(TIMESCALE 100ps)") -> str:
    cell = f'(CELL (CELLTYPE "BUF") (INSTANCE \\u\\/a) {body})'
    return (
        f'(DELAYFILE (SDFVERSION "3.0") (DESIGN "top") (DIVIDER /) {timescale} {cell})'
    )


class TestRoutedDelays:
    """Delays come out per pin, in nanoseconds, at the selected corner and edge."""

    @pytest.mark.parametrize(
        ("corner", "edge", "sink", "output"),
        [
            (Corner.MAX, EdgeSelect.WORST, 0.6, 1.2),
            (Corner.MAX, EdgeSelect.FALL, 0.6, 1.2),
            (Corner.MAX, EdgeSelect.RISE, 0.3, 0.9),
            (Corner.MIN, EdgeSelect.WORST, 0.4, 1.0),
        ],
    )
    def test_reads_the_selected_value(
        self, corner: Corner, edge: EdgeSelect, sink: float, output: float
    ) -> None:
        sdf = parse_sdf(
            _sdf(
                "(DELAY (ABSOLUTE"
                " (PORT A (1:2:3) (4:5:6))"
                " (IOPATH A Y (7:8:9) (8:9:10))"
                " (IOPATH B Y (1:1:1) (10:11:12))))"
            )
        )
        delays = routed_delays(sdf, corner=corner, edge=edge)
        assert delays.sinks == {("u/a", "A"): pytest.approx(sink)}
        assert delays.outputs == {("u/a", "Y"): pytest.approx(output)}

    def test_skips_an_empty_rvalue(self) -> None:
        sdf = parse_sdf(_sdf("(DELAY (ABSOLUTE (PORT A () (1:2:3))))"))
        delays = routed_delays(sdf, edge=EdgeSelect.RISE)
        assert delays.sinks == {}

    @pytest.mark.parametrize(
        ("text", "message"),
        [
            (_sdf("", timescale=""), "no TIMESCALE"),
            (_sdf("(DELAY (INCREMENT (PORT A (1:2:3))))"), "INCREMENT"),
            (_sdf("(DELAY (ABSOLUTE (INTERCONNECT x/Y z/A (1:2:3))))"), "INTERCONNECT"),
        ],
    )
    def test_refuses_what_has_no_transport_form(self, text: str, message: str) -> None:
        with pytest.raises(AnnotationError, match=message):
            routed_delays(parse_sdf(text))


def _fanout() -> YosysModuleJson:
    """``u0.Y`` drives ``u1.A`` and ``u2.A`` on bit 2."""

    def cell(a: int, y: int) -> dict[str, object]:
        return {
            "hide_name": 0,
            "type": "BUF",
            "parameters": {},
            "attributes": {},
            "port_directions": {"A": "input", "Y": "output"},
            "connections": {"A": [a], "Y": [y]},
        }

    module = {
        "attributes": {},
        "ports": {},
        "cells": {"u0": cell(5, 2), "u1": cell(2, 3), "u2": cell(2, 4)},
        "netnames": {},
    }
    return cast("YosysModuleJson", module)


class TestInsertTransportBuffers:
    """Each sink gets its own buffer, carrying driver plus routing delay."""

    @pytest.mark.parametrize(
        ("sinks", "loads"),
        [
            ({("u1", "A"): 0.25, ("u2", "A"): 0.5}, {"u1": 1.25, "u2": 1.5}),
            ({("u1", "A"): 0.25}, {"u1": 1.25, "u2": 1.0}),
        ],
    )
    def test_sinks_of_one_net_keep_their_own_delay(
        self, sinks: dict[tuple[str, str], float], loads: dict[str, float]
    ) -> None:
        module = _fanout()
        delays = RoutedDelays(sinks=sinks, outputs={("u0", "Y"): 1.0})
        inserted = insert_transport_buffers(module, delays, buffer="tb")
        by_load = {}
        for name, delay in inserted.items():
            (new_bit,) = module["cells"][name]["connections"]["Y"]
            for load in loads:
                if module["cells"][load]["connections"]["A"] == [new_bit]:
                    by_load[load] = delay
        assert by_load == pytest.approx(loads)

    def test_delay_parameter_is_in_femtoseconds(self) -> None:
        module = _fanout()
        delays = RoutedDelays(sinks={("u1", "A"): 0.25}, outputs={})
        inserted = insert_transport_buffers(module, delays, buffer="tb")
        values = {
            int(module["cells"][name]["parameters"]["DLY_FS"], 2) for name in inserted
        }
        assert 250_000 in values

    def test_refuses_a_negative_delay(self) -> None:
        delays = RoutedDelays(sinks={("u1", "A"): -1.0}, outputs={})
        with pytest.raises(AnnotationError, match="fs form"):
            insert_transport_buffers(_fanout(), delays)


_NETLIST = """
module top(input a, output y);
  wire w;
  BUF u0 (.A(a), .Y(w));
  BUF u1 (.A(w), .Y(y));
endmodule
"""
_VIEW = "module BUF(input A, output Y); endmodule\n"
_MODEL = "module BUF(input A, output Y); assign Y = A; endmodule\n"
# A 1 ns pulse through a 5 ns route, which an inertial delay would swallow.
_BENCH = """
`timescale 1ns/1ps
module bench;
  reg a = 0;
  wire y;
  top dut (.a(a), .y(y));
  initial begin
    #10 a = 1;
    #1 a = 0;
    #20 $finish(0);
  end
  always @(y) $display("%0t %b", $realtime, y);
endmodule
"""
_ROUTE = (
    '(DELAYFILE (SDFVERSION "3.0") (DESIGN "top") (DIVIDER /) (TIMESCALE 1ns)'
    ' (CELL (CELLTYPE "BUF") (INSTANCE u1) (DELAY (ABSOLUTE (PORT A (5:5:5))))))'
)


@pytest.fixture
def design(tmp_path: Path) -> tuple[Path, Path, Path]:
    sdf, netlist, view = tmp_path / "d.sdf", tmp_path / "d.v", tmp_path / "view.v"
    sdf.write_text(_ROUTE)
    netlist.write_text(_NETLIST)
    view.write_text(_VIEW)
    return sdf, netlist, view


@requires_yosys
@requires_iverilog
def test_pulse_narrower_than_the_route_survives(
    tmp_path: Path, design: tuple[Path, Path, Path]
) -> None:
    sdf, netlist, view = design
    baked = tmp_path / "baked.v"
    annotate_transport(sdf, netlist, top="top", library=[view], output_path=baked)
    (tmp_path / "model.v").write_text(_MODEL)
    (tmp_path / "bench.v").write_text(_BENCH)
    sim = tmp_path / "a.out"
    subprocess.run(
        [
            "iverilog",
            "-g2012",
            "-s",
            "bench",
            "-o",
            str(sim),
            str(tmp_path / "bench.v"),
            str(baked),
            str(tmp_path / "model.v"),
        ],
        check=True,
    )
    result = subprocess.run(
        ["vvp", str(sim)], capture_output=True, text=True, check=True
    )
    assert result.stdout.splitlines()[-2:] == ["15000 1", "16000 0"]


@requires_yosys
def test_cli_writes_the_annotated_netlist(
    tmp_path: Path, design: tuple[Path, Path, Path]
) -> None:
    sdf, netlist, view = design
    out = tmp_path / "out.v"
    result = CliRunner().invoke(
        app,
        [
            "annotate-transport",
            str(sdf),
            str(netlist),
            "--top",
            "top",
            "--library",
            str(view),
            "--output",
            str(out),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "module sdf_transport_buffer" in out.read_text()
