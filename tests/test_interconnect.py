"""Tests for PORT to INTERCONNECT conversion."""

import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.model import (
    DelayMode,
    DelayValues,
    Divider,
    Interconnect,
    Iopath,
    Port,
    PortSpec,
    SDFFile,
    SDFHeader,
    Timescale,
    Values,
)
from sdf_toolkit.io.annotate import (
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
        top_cell = result.cells["port_top"]["port_top"]
        assert len(top_cell) == 4
        assert all(isinstance(e, Interconnect) for e in top_cell)

    def test_delay_is_carried_over(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        by_sink = {
            e.load.port: e
            for e in result.cells["port_top"]["port_top"]
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
        with pytest.raises(DriverResolutionError, match="no unique driver"):
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
