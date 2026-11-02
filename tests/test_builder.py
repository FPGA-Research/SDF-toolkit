"""Tests for SDFBuilder and CellBuilder."""

import pytest

from sdf_toolkit.core.builder import CellBuilder, SDFBuilder
from sdf_toolkit.core.model import (
    DelayMode,
    DelayValues,
    Device,
    EdgeType,
    Entry,
    Hold,
    Interconnect,
    Iopath,
    PathConstraint,
    Period,
    Port,
    PortSpec,
    Recovery,
    Removal,
    Setup,
    SetupHold,
    Timescale,
    TimingPortSpec,
    Values,
    Width,
)

_DELAY = DelayValues.of(Values.uniform(1.0))
_LIMIT = Values.uniform(0.5)
_DATA = TimingPortSpec("D")
_CLK = TimingPortSpec("CLK", edge=EdgeType.POSEDGE)


def _iopath(input_pin: str) -> Iopath:
    return Iopath(
        input=PortSpec(input_pin),
        output=PortSpec("Q"),
        mode=DelayMode.ABSOLUTE,
        delay=_DELAY,
    )


class TestAddEntry:
    def test_appends_in_order(self):
        cb = SDFBuilder().add_cell("cell", "inst")
        entries = [_iopath("A"), _iopath("B")]
        for entry in entries:
            cb.add_entry(entry)
        assert cb.build().cells["cell"]["inst"] == entries

    def test_keeps_repeated_arcs(self):
        cb = SDFBuilder().add_cell("cell", "inst")
        for _ in range(3):
            cb.add_entry(_iopath("CP"))
        assert len(cb.build().cells["cell"]["inst"]) == 3


_ADDERS: list[tuple[str, dict[str, object], Entry]] = [
    (
        "add_iopath",
        {"input": PortSpec("A"), "output": PortSpec("Y"), "delay": _DELAY},
        Iopath(
            input=PortSpec("A"),
            output=PortSpec("Y"),
            mode=DelayMode.ABSOLUTE,
            delay=_DELAY,
        ),
    ),
    (
        "add_port",
        {"port": PortSpec("A"), "delay": _DELAY, "mode": DelayMode.INCREMENT},
        Port(port=PortSpec("A"), mode=DelayMode.INCREMENT, delay=_DELAY),
    ),
    (
        "add_interconnect",
        {"source": PortSpec("u1.Y"), "load": PortSpec("u2.A"), "delay": _DELAY},
        Interconnect(
            source=PortSpec("u1.Y"),
            load=PortSpec("u2.A"),
            mode=DelayMode.ABSOLUTE,
            delay=_DELAY,
        ),
    ),
    (
        "add_device",
        {"output": None, "delay": _DELAY},
        Device(output=None, mode=DelayMode.ABSOLUTE, delay=_DELAY),
    ),
    (
        "add_setup",
        {"data": _DATA, "reference": _CLK, "limit": _LIMIT},
        Setup(data=_DATA, reference=_CLK, limit=_LIMIT),
    ),
    (
        "add_hold",
        {"data": _DATA, "reference": _CLK, "limit": _LIMIT},
        Hold(data=_DATA, reference=_CLK, limit=_LIMIT),
    ),
    (
        "add_recovery",
        {"data": _DATA, "reference": _CLK, "limit": _LIMIT},
        Recovery(data=_DATA, reference=_CLK, limit=_LIMIT),
    ),
    (
        "add_removal",
        {"data": _DATA, "reference": _CLK, "limit": _LIMIT},
        Removal(data=_DATA, reference=_CLK, limit=_LIMIT),
    ),
    (
        "add_setuphold",
        {"data": _DATA, "reference": _CLK, "setup": _LIMIT, "hold": _LIMIT},
        SetupHold(data=_DATA, reference=_CLK, setup=_LIMIT, hold=_LIMIT),
    ),
    (
        "add_width",
        {"port": _CLK, "limit": _LIMIT},
        Width(port=_CLK, limit=_LIMIT),
    ),
    (
        "add_period",
        {"port": _CLK, "limit": _LIMIT},
        Period(port=_CLK, limit=_LIMIT),
    ),
    (
        "add_path_constraint",
        {"ports": ("A", "B"), "rise": _LIMIT, "fall": _LIMIT},
        PathConstraint(ports=("A", "B"), rise=_LIMIT, fall=_LIMIT),
    ),
]


class TestAdders:
    @pytest.mark.parametrize(
        ("adder", "kwargs", "expected"), _ADDERS, ids=[a[0] for a in _ADDERS]
    )
    def test_adder_builds_entry(self, adder, kwargs, expected):
        cb = SDFBuilder().add_cell("cell", "inst")
        add = {
            "add_iopath": cb.add_iopath,
            "add_port": cb.add_port,
            "add_interconnect": cb.add_interconnect,
            "add_device": cb.add_device,
            "add_setup": cb.add_setup,
            "add_hold": cb.add_hold,
            "add_recovery": cb.add_recovery,
            "add_removal": cb.add_removal,
            "add_setuphold": cb.add_setuphold,
            "add_width": cb.add_width,
            "add_period": cb.add_period,
            "add_path_constraint": cb.add_path_constraint,
        }[adder]
        result = add(**kwargs)
        assert isinstance(result, CellBuilder)
        assert result.build().cells["cell"]["inst"] == [expected]


class TestSDFBuilder:
    def test_set_header_merges_calls(self):
        sdf = (
            SDFBuilder()
            .set_header(sdfversion="4.0")
            .set_header(timescale=Timescale.parse("1ns"))
            .add_cell("BUF", "b0")
            .add_iopath(input=PortSpec("A"), output=PortSpec("Y"), delay=_DELAY)
            .add_cell("INV", "i0")
            .add_iopath(input=PortSpec("A"), output=PortSpec("Z"), delay=_DELAY)
            .build()
        )
        assert sdf.header.sdfversion == "4.0"
        assert sdf.header.timescale == Timescale.parse("1ns")
        assert set(sdf.cells) == {"BUF", "INV"}

    def test_add_cell_continues_instance(self):
        builder = SDFBuilder()
        builder.add_cell("BUF", "b0").add_entry(_iopath("A"))
        builder.add_cell("BUF", "b0").add_entry(_iopath("B"))
        assert builder.build().cells["BUF"]["b0"] == [_iopath("A"), _iopath("B")]
