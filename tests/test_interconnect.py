"""Tests for PORT to INTERCONNECT conversion."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.delays import Interconnect, Iopath, Port, PortSpec
from sdf_toolkit.core.enums import DelayMode, Divider
from sdf_toolkit.core.sdffile import SDFFile, SDFHeader
from sdf_toolkit.core.values import DelayValues, Timescale, Values
from sdf_toolkit.io.writer import emit_sdf
from sdf_toolkit.io.yosys import (
    PortDirection,
    YosysCell,
    YosysDesign,
    YosysModule,
    YosysPort,
    parse_yosys_json,
    run_yosys,
)
from sdf_toolkit.parser.parser import parse_sdf
from sdf_toolkit.transform.interconnect import (
    DriverResolutionError,
    InterconnectConflictError,
    port_to_interconnect,
)

DATA_DIR = (Path(__file__).parent / "data").resolve()
HAS_YOSYS = shutil.which("yosys") is not None


def _delay(value: float) -> DelayValues:
    return DelayValues.of(Values.uniform(value))


def _port(pin: str, value: float) -> Port:
    return Port(port=PortSpec(pin), mode=DelayMode.ABSOLUTE, delay=_delay(value))


def _iopath(input_pin: str, output_pin: str, value: float) -> Iopath:
    return Iopath(
        input=PortSpec(input_pin),
        output=PortSpec(output_pin),
        mode=DelayMode.ABSOLUTE,
        delay=_delay(value),
    )


def _sample_sdf() -> SDFFile:
    """SDF matching tests/data/port_netlist.v: buf -> and -> inv with PORTs.

    The buffer (u_buf) has no IOPATH, so its output pin is only discoverable by
    elimination.
    """
    return SDFFile(
        header=SDFHeader(
            design="port_top",
            divider=Divider.SLASH,
            timescale=Timescale.parse("1ns"),
        ),
        cells={
            "BUF": {"u_buf": [_port("a", 0.1)]},
            "AND2": {
                "u_and": [
                    _port("i1", 0.2),
                    _port("i2", 0.3),
                    _iopath("i1", "z", 0.5),
                    _iopath("i2", "z", 0.5),
                ]
            },
            "INV": {"u_inv": [_port("i", 0.4), _iopath("i", "z", 0.6)]},
        },
    )


def _sample_design() -> YosysDesign:
    """Netlist for the sample SDF, mirroring tests/data/port_netlist.v.

    Bit assignment: a=2, b=3, x_buf=4, x_and=5, y=6.
    """
    return YosysDesign(
        modules={
            "port_top": YosysModule(
                name="port_top",
                ports={
                    "a": YosysPort("a", PortDirection.INPUT, [2]),
                    "b": YosysPort("b", PortDirection.INPUT, [3]),
                    "y": YosysPort("y", PortDirection.OUTPUT, [6]),
                },
                cells={
                    "u_buf": YosysCell("u_buf", "BUF", {"a": [2], "y": [4]}),
                    "u_and": YosysCell(
                        "u_and", "AND2", {"i1": [4], "i2": [3], "z": [5]}
                    ),
                    "u_inv": YosysCell("u_inv", "INV", {"i": [5], "z": [6]}),
                },
            )
        }
    )


def _interconnects(sdf: SDFFile) -> dict[str, str]:
    """Return {sink_path: source_path} for every INTERCONNECT in *sdf*."""
    pairs: dict[str, str] = {}
    for instances in sdf.cells.values():
        for entries in instances.values():
            for entry in entries:
                if isinstance(entry, Interconnect):
                    pairs[entry.load.port] = entry.source.port
    return pairs


class TestPortToInterconnect:
    """Unit tests using a hand-built netlist (no Yosys required)."""

    def test_drivers_resolved(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        assert _interconnects(result) == {
            "u_buf/a": "a",  # top input port drives the buffer
            "u_and/i1": "u_buf/y",  # untimed buffer output, found by elimination
            "u_and/i2": "b",  # top input port
            "u_inv/i": "u_and/z",  # AND2 output identified by its IOPATH
        }

    def test_port_entries_removed(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        for instances in result.cells.values():
            for entries in instances.values():
                for entry in entries:
                    assert not isinstance(entry, Port)

    def test_interconnects_placed_on_top_cell(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        # An empty INSTANCE names the top-level design in SDF 3.0.
        assert list(result.cells["port_top"]) == [""]
        top_cell = result.cells["port_top"][""]
        assert len(top_cell) == 4
        assert all(isinstance(e, Interconnect) for e in top_cell)

    def test_delay_is_carried_over(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        by_sink = {
            e.load.port: e
            for e in result.cells["port_top"][""]
            if isinstance(e, Interconnect)
        }
        assert by_sink["u_buf/a"].delay == _delay(0.1)
        assert by_sink["u_inv/i"].delay == _delay(0.4)

    def test_input_sdf_not_mutated(self) -> None:
        sdf = _sample_sdf()
        port_to_interconnect(sdf, _sample_design())
        # The original still has its PORT entries.
        assert sdf.cells["BUF"]["u_buf"] == [_port("a", 0.1)]

    def test_default_top_module_from_header(self) -> None:
        # No explicit top_module: falls back to header DESIGN ("port_top").
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        assert "port_top" in result.cells

    def test_missing_top_module_raises(self) -> None:
        sdf = _sample_sdf()
        sdf = replace(sdf, header=replace(sdf.header, design=None))
        with pytest.raises(DriverResolutionError, match="no DESIGN"):
            port_to_interconnect(sdf, _sample_design())

    def test_unknown_top_module_raises(self) -> None:
        with pytest.raises(DriverResolutionError, match="not found in netlist"):
            port_to_interconnect(_sample_sdf(), _sample_design(), top_module="nope")

    def test_instance_absent_from_netlist_raises(self) -> None:
        design = _sample_design()
        del design.modules["port_top"].cells["u_inv"]
        with pytest.raises(DriverResolutionError, match="u_inv/i"):
            port_to_interconnect(_sample_sdf(), design)

    def test_ambiguous_driver_raises(self) -> None:
        # Two output pins on the same net leave the driver ambiguous.
        design = _sample_design()
        design.modules["port_top"].cells["u_extra"] = YosysCell(
            "u_extra",
            "AND2",
            {"z": [2]},  # a second driver on net 'a' (bit 2)
        )
        with pytest.raises(DriverResolutionError, match="several drivers"):
            port_to_interconnect(_sample_sdf(), design)


def _rename_instance(
    sdf: SDFFile, design: YosysDesign, *, old: str, sdf_name: str, netlist_name: str
) -> None:
    """Rename instance *old* to *sdf_name* in the SDF and *netlist_name* in Yosys."""
    for instances in sdf.cells.values():
        if old in instances:
            instances[sdf_name] = instances.pop(old)
    cells = design.modules["port_top"].cells
    cell = cells.pop(old)
    cells[netlist_name] = replace(cell, name=netlist_name)


_ESCAPED_NAMES = [
    pytest.param(r"u_inv\[0\]", "u_inv[0]", id="bit-index"),
    pytest.param(r"g\[1\]\.u_inv", "g[1].u_inv", id="generate-loop"),
    pytest.param(r"u\$inv", "u$inv", id="dollar"),
]


class TestEscapedIdentifiers:
    """SDF keeps names escaped; Yosys reports them bare."""

    @pytest.mark.parametrize(("sdf_name", "netlist_name"), _ESCAPED_NAMES)
    def test_escaped_sink_instance_matches_netlist(
        self, sdf_name: str, netlist_name: str
    ) -> None:
        sdf, design = _sample_sdf(), _sample_design()
        _rename_instance(
            sdf, design, old="u_inv", sdf_name=sdf_name, netlist_name=netlist_name
        )
        result = port_to_interconnect(sdf, design)
        assert _interconnects(result)[f"{sdf_name}/i"] == "u_and/z"

    @pytest.mark.parametrize(("sdf_name", "netlist_name"), _ESCAPED_NAMES)
    def test_driver_path_is_escaped(self, sdf_name: str, netlist_name: str) -> None:
        sdf, design = _sample_sdf(), _sample_design()
        _rename_instance(
            sdf, design, old="u_and", sdf_name=sdf_name, netlist_name=netlist_name
        )
        result = port_to_interconnect(sdf, design)
        assert _interconnects(result)["u_inv/i"] == f"{sdf_name}/z"


class TestDelayModes:
    @pytest.mark.parametrize(
        "modes",
        [
            (DelayMode.ABSOLUTE, DelayMode.INCREMENT),
            (DelayMode.INCREMENT, DelayMode.ABSOLUTE),
        ],
    )
    def test_absolute_and_increment_port_on_one_pin_both_survive(
        self, modes: tuple[DelayMode, DelayMode]
    ) -> None:
        sdf = _sample_sdf()
        sdf.cells["INV"]["u_inv"] = [
            replace(_port("i", value), mode=mode)
            for value, mode in zip((0.4, 0.05), modes, strict=True)
        ]
        top = port_to_interconnect(sdf, _sample_design()).cells["port_top"][""]
        on_pin = [e for e in top if isinstance(e, Interconnect)]
        on_pin = [e for e in on_pin if e.load.port == "u_inv/i"]
        assert [e.mode for e in on_pin] == list(modes)
        assert len(top) == 5


def _top_interconnect(mode: DelayMode, value: float) -> Interconnect:
    return Interconnect(
        source=PortSpec("u_and/z"),
        load=PortSpec("u_inv/i"),
        mode=mode,
        delay=_delay(value),
    )


class TestTopCell:
    @pytest.mark.parametrize(
        ("mode", "conflicts"),
        [(DelayMode.ABSOLUTE, True), (DelayMode.INCREMENT, False)],
    )
    def test_existing_interconnect_conflicts_only_on_same_arc(
        self, mode: DelayMode, conflicts: bool
    ) -> None:
        # Every generated INTERCONNECT is ABSOLUTE, like the sample PORTs.
        sdf = _sample_sdf()
        existing = _top_interconnect(mode, 5.0)
        sdf.cells["port_top"] = {"": [existing]}
        if conflicts:
            with pytest.raises(InterconnectConflictError, match="u_inv/i"):
                port_to_interconnect(sdf, _sample_design())
        else:
            top = port_to_interconnect(sdf, _sample_design()).cells["port_top"][""]
            assert top[0] == existing
            assert len(top) == 5

    def test_written_top_cell_has_empty_instance(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        text = emit_sdf(result)
        assert '(CELLTYPE "port_top")' in text
        assert "(INSTANCE port_top)" not in text
        assert parse_sdf(text).cells["port_top"][""] == result.cells["port_top"][""]


class TestDriverRoles:
    def test_unknown_second_driver_beside_known_driver_raises(self) -> None:
        # u_extra is untimed and has no netlist module, so its pin on net
        # x_and (bit 5) could be a second driver of u_inv/i.
        design = _sample_design()
        design.modules["port_top"].cells["u_extra"] = YosysCell(
            "u_extra", "UNTIMED", {"q": [5]}
        )
        with pytest.raises(DriverResolutionError, match=r"u_extra/q"):
            port_to_interconnect(_sample_sdf(), design)

    @pytest.mark.parametrize(
        ("direction", "resolves"),
        [(PortDirection.INPUT, True), (PortDirection.OUTPUT, False)],
    )
    def test_netlist_port_direction_gives_role(
        self, direction: PortDirection, resolves: bool
    ) -> None:
        # The UNTIMED leaf module declares the direction of u_extra/q.
        design = _sample_design()
        design.modules["port_top"].cells["u_extra"] = YosysCell(
            "u_extra", "UNTIMED", {"q": [5]}
        )
        design.modules["UNTIMED"] = YosysModule(
            name="UNTIMED", ports={"q": YosysPort("q", direction, [])}
        )
        if resolves:
            result = port_to_interconnect(_sample_sdf(), design)
            assert _interconnects(result)["u_inv/i"] == "u_and/z"
        else:
            with pytest.raises(DriverResolutionError, match="several drivers"):
                port_to_interconnect(_sample_sdf(), design)

    def test_constant_pin_raises(self) -> None:
        design = _sample_design()
        design.modules["port_top"].cells["u_inv"].connections["i"] = ["1"]
        with pytest.raises(DriverResolutionError, match="constant"):
            port_to_interconnect(_sample_sdf(), design)


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
class TestPortToInterconnectWithYosys:
    """Integration tests parsing the fixture netlist with Yosys."""

    def test_fixture_round_trip(self) -> None:
        sdf = parse_sdf((DATA_DIR / "port_netlist.sdf").read_text())
        design = parse_yosys_json(run_yosys(DATA_DIR / "port_netlist.v"))
        result = port_to_interconnect(sdf, design)
        assert _interconnects(result) == {
            "u_buf/a": "a",
            "u_and/i1": "u_buf/y",
            "u_and/i2": "b",
            "u_inv/i": "u_and/z",
        }


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
class TestPortToInterconnectCli:
    """Test the CLI port-to-interconnect command."""

    def test_stdout_output(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "port-to-interconnect",
                str(DATA_DIR / "port_netlist.sdf"),
                str(DATA_DIR / "port_netlist.v"),
            ],
        )
        assert result.exit_code == 0
        assert "(INTERCONNECT a u_buf/a" in result.stdout
        assert "(INTERCONNECT u_buf/y u_and/i1" in result.stdout
        assert "(PORT " not in result.stdout

    def test_file_output(self, tmp_path: Path) -> None:
        output = tmp_path / "out.sdf"
        runner = CliRunner()
        result = runner.invoke(
            app,
            [
                "port-to-interconnect",
                str(DATA_DIR / "port_netlist.sdf"),
                str(DATA_DIR / "port_netlist.v"),
                "-o",
                str(output),
            ],
        )
        assert result.exit_code == 0
        assert output.exists()
        assert "INTERCONNECT" in output.read_text()


GENERATE_NETLIST = """\
(* blackbox *)
module INV(input i, output z);
endmodule

module gen_top(input a, output [1:0] y);
    wire [1:0] mid;
    genvar k;
    generate for (k = 0; k < 2; k = k + 1) begin : g
        INV u_inv (.i(a), .z(mid[k]));
        INV \\u_out[0] (.i(mid[k]), .z(y[k]));
    end endgenerate
endmodule
"""

GENERATE_SDF = r"""(DELAYFILE (SDFVERSION "3.0") (DESIGN "gen_top") (DIVIDER /)
 (CELL (CELLTYPE "INV") (INSTANCE g\[0\]\.u_inv) (DELAY (ABSOLUTE (PORT i (0.1)))))
 (CELL (CELLTYPE "INV") (INSTANCE g\[1\]\.u_out\[0\])
  (DELAY (ABSOLUTE (PORT i (0.2)))))
)"""


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
def test_generate_loop_instances_match_yosys(tmp_path: Path) -> None:
    netlist = tmp_path / "gen.v"
    netlist.write_text(GENERATE_NETLIST)
    design = parse_yosys_json(run_yosys(netlist))
    result = port_to_interconnect(parse_sdf(GENERATE_SDF), design)
    assert _interconnects(result) == {
        r"g\[0\]\.u_inv/i": "a",
        r"g\[1\]\.u_out\[0\]/i": r"g\[1\]\.u_inv/z",
    }


BUS_SRC_MODULE = """\
(* blackbox *)
module SRC(output {src_range} DOUT);
endmodule
"""

BUS_NETLIST = """\
(* blackbox *)
module BUF(input A, output Y);
endmodule

module bus_top(input {top_range} a, output y0, output y1);
    wire [3:0] d;
    SRC u_src (.DOUT(d));
    BUF u_b0 (.A(d[3]), .Y(y0));
    BUF u_b1 (.A({top_bit}), .Y(y1));
endmodule
"""

BUS_SDF = """(DELAYFILE (SDFVERSION "3.0") (DESIGN "bus_top") (DIVIDER /)
 (CELL (CELLTYPE "BUF") (INSTANCE u_b0)
  (DELAY (ABSOLUTE (IOPATH A Y (1)) (PORT A (0.1)))))
 (CELL (CELLTYPE "BUF") (INSTANCE u_b1)
  (DELAY (ABSOLUTE (IOPATH A Y (1)) (PORT A (0.2)))))
)"""


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
@pytest.mark.parametrize(
    ("src_range", "src_bit", "top_range", "top_bit"),
    [
        ("[3:0]", "DOUT[3]", "[1:0]", "a[1]"),
        ("[0:3]", "DOUT[0]", "[0:1]", "a[0]"),
        ("[7:4]", "DOUT[7]", "[5:4]", "a[5]"),
    ],
    ids=["descending", "ascending", "offset"],
)
def test_bus_driver_keeps_its_bit_index(
    tmp_path: Path, src_range: str, src_bit: str, top_range: str, top_bit: str
) -> None:
    # d[3] is the MSB of d, so it drives the MSB of DOUT in every range.
    netlist = tmp_path / "bus.v"
    netlist.write_text(
        BUS_SRC_MODULE.format(src_range=src_range)
        + BUS_NETLIST.format(top_range=top_range, top_bit=top_bit)
    )
    design = parse_yosys_json(run_yosys(netlist))
    result = port_to_interconnect(parse_sdf(BUS_SDF), design)
    assert _interconnects(result) == {"u_b0/A": f"u_src/{src_bit}", "u_b1/A": top_bit}


@pytest.mark.skipif(not HAS_YOSYS, reason="Yosys not installed")
def test_bus_driver_of_undefined_module_raises(tmp_path: Path) -> None:
    netlist = tmp_path / "bus.v"
    netlist.write_text(BUS_NETLIST.format(top_range="[1:0]", top_bit="a[1]"))
    design = parse_yosys_json(run_yosys(netlist))
    with pytest.raises(DriverResolutionError, match="u_src/DOUT.*SRC"):
        port_to_interconnect(parse_sdf(BUS_SDF), design)
