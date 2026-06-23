"""Tests for PORT to INTERCONNECT conversion."""

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.builder import make_iopath, make_port
from sdf_toolkit.core.model import (
    DelayPaths,
    EntryType,
    Iopath,
    Port,
    SDFFile,
    SDFHeader,
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


def _triple(value: float) -> DelayPaths:
    """Return a nominal DelayPaths with a single min:typ:max value."""
    return DelayPaths(nominal=Values(value, value, value))


def _port(pin: str, value: float) -> Port:
    """Build an absolute PORT entry on *pin* via the builder factory."""
    entry = make_port(pin, _triple(value))
    entry.is_absolute = True
    return entry


def _iopath(from_pin: str, to_pin: str, value: float) -> Iopath:
    """Build an absolute IOPATH entry via the builder factory."""
    entry = make_iopath(from_pin, to_pin, _triple(value))
    entry.is_absolute = True
    return entry


def _sample_sdf() -> SDFFile:
    """SDF matching tests/data/port_netlist.v: buf -> and -> inv with PORTs.

    The buffer (u_buf) has no IOPATH, so its output pin is only discoverable by
    elimination.
    """
    return SDFFile(
        header=SDFHeader(design="port_top", divider="/", timescale="1ns"),
        cells={
            "BUF": {"u_buf": {"port_a": _port("a", 0.1)}},
            "AND2": {
                "u_and": {
                    "port_i1": _port("i1", 0.2),
                    "port_i2": _port("i2", 0.3),
                    "iopath_i1_z": _iopath("i1", "z", 0.5),
                    "iopath_i2_z": _iopath("i2", "z", 0.5),
                }
            },
            "INV": {
                "u_inv": {
                    "port_i": _port("i", 0.4),
                    "iopath_i_z": _iopath("i", "z", 0.6),
                }
            },
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
            for entry in entries.values():
                if entry.type == EntryType.INTERCONNECT:
                    pairs[entry.to_pin] = entry.from_pin
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
                for entry in entries.values():
                    assert entry.type != EntryType.PORT

    def test_interconnects_placed_on_top_cell(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        top_cell = result.cells["port_top"]["port_top"]
        assert len(top_cell) == 4
        assert all(e.type == EntryType.INTERCONNECT for e in top_cell.values())

    def test_delay_is_carried_over(self) -> None:
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        by_sink = {e.to_pin: e for e in result.cells["port_top"]["port_top"].values()}
        assert by_sink["u_buf/a"].delay_paths.nominal.max == 0.1
        assert by_sink["u_inv/i"].delay_paths.nominal.max == 0.4

    def test_input_sdf_not_mutated(self) -> None:
        sdf = _sample_sdf()
        port_to_interconnect(sdf, _sample_design())
        # The original still has its PORT entries.
        assert sdf.cells["BUF"]["u_buf"]["port_a"].type == EntryType.PORT

    def test_default_top_module_from_header(self) -> None:
        # No explicit top_module: falls back to header DESIGN ("port_top").
        result = port_to_interconnect(_sample_sdf(), _sample_design())
        assert "port_top" in result.cells

    def test_missing_top_module_raises(self) -> None:
        sdf = _sample_sdf()
        sdf.header.design = None
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
