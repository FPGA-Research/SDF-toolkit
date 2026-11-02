"""How SDF entries become timing-graph arcs."""

from collections.abc import Callable

import pytest

from sdf_toolkit.core.builder import CellBuilder, SDFBuilder
from sdf_toolkit.core.model import (
    Cond,
    DelayMode,
    DelayValues,
    Divider,
    EdgeType,
    EntryType,
    PortSpec,
    TimingPortSpec,
    Values,
)
from sdf_toolkit.core.pathgraph import (
    ARC_ATTR,
    ConditionalPinDelayError,
    IncrementWithoutBaseError,
    NetDelayError,
    TimingEdge,
    TimingGraph,
    UnplacedDeviceDelayError,
    WildcardInstanceError,
    critical_path,
)
from sdf_toolkit.parser.parser import parse_sdf

_A, _Y, _Z = PortSpec("A"), PortSpec("Y"), PortSpec("Z")
_ABS, _INC = DelayMode.ABSOLUTE, DelayMode.INCREMENT


def _d(value: float) -> DelayValues:
    return DelayValues.of(Values.uniform(value))


def _builder(divider: Divider = Divider.SLASH) -> SDFBuilder:
    return SDFBuilder().set_header(divider=divider)


def _delay(graph: TimingGraph, source: str, sink: str) -> float:
    cp = critical_path(graph, source, sink)
    assert cp is not None
    assert cp.scalar is not None
    return cp.scalar


class TestInterconnectScope:
    @pytest.mark.parametrize("divider", list(Divider))
    def test_pins_are_scoped_to_the_cell_instance(self, divider: Divider) -> None:
        sep = str(divider)
        sdf = (
            _builder(divider)
            .add_cell("SUB", "u0")
            .add_interconnect(
                source=PortSpec(f"a{sep}Y"), load=PortSpec(f"b{sep}A"), delay=_d(1.0)
            )
            .build()
        )
        (edge,) = TimingGraph(sdf).edges()
        assert (edge.source, edge.sink) == (f"u0{sep}a{sep}Y", f"u0{sep}b{sep}A")

    def test_top_level_pins_stay_as_written(self) -> None:
        sdf = (
            _builder()
            .add_cell("TOP", "")
            .add_interconnect(source=PortSpec("a/Y"), load=PortSpec("b/A"), delay=_d(1))
            .build()
        )
        assert TimingGraph(sdf).nodes() == {"a/Y", "b/A"}

    @pytest.mark.parametrize(
        "add",
        [
            pytest.param(
                lambda c: c.add_iopath(input=_A, output=_Y, delay=_d(1)), id="iopath"
            ),
            pytest.param(
                lambda c: c.add_interconnect(source=_A, load=_Y, delay=_d(1)),
                id="interconnect",
            ),
            pytest.param(
                lambda c: c.add_setup(
                    data=TimingPortSpec("D"),
                    reference=TimingPortSpec("CLK"),
                    limit=Values.uniform(1),
                ),
                id="setup",
            ),
            pytest.param(lambda c: c.add_port(port=_A, delay=_d(1)), id="port"),
        ],
    )
    def test_wildcard_instance_raises(
        self, add: Callable[[CellBuilder], CellBuilder]
    ) -> None:
        sdf = add(_builder().add_cell("BUF", "*")).build()
        with pytest.raises(WildcardInstanceError, match=r"BUF.*INSTANCE \*"):
            TimingGraph(sdf)


class TestTopLevelIopath:
    def test_empty_instance_leaves_pins_unqualified(self) -> None:
        sdf = (
            _builder()
            .add_cell("TOP", "")
            .add_iopath(input=_A, output=_Y, delay=_d(2.0))
            .build()
        )
        assert _delay(TimingGraph(sdf), "A", "Y") == 2.0


def _wired_buffer(builder: SDFBuilder) -> CellBuilder:
    """``src/Y -> u/A`` (0.5) and ``u`` IOPATH ``A -> Y`` (1.0), open on ``u``."""
    builder.add_cell("TOP", "").add_interconnect(
        source=PortSpec("src/Y"), load=PortSpec("u/A"), delay=_d(0.5)
    )
    return builder.add_cell("BUF", "u").add_iopath(input=_A, output=_Y, delay=_d(1.0))


def _port(mode: DelayMode, value: float) -> Callable[[CellBuilder], CellBuilder]:
    return lambda c: c.add_port(port=_A, delay=_d(value), mode=mode)


def _device(
    mode: DelayMode, value: float, output: PortSpec | None = _Y
) -> Callable[[CellBuilder], CellBuilder]:
    return lambda c: c.add_device(output=output, delay=_d(value), mode=mode)


class TestPinDelays:
    """PORT acts on the 0.5 wire into ``u/A``, DEVICE on the 1.0 IOPATH."""

    @pytest.mark.parametrize(
        ("adds", "expected"),
        [
            pytest.param([], 1.5, id="none"),
            pytest.param([_port(_ABS, 0.25)], 1.25, id="port-absolute"),
            pytest.param([_port(_INC, 0.25)], 1.75, id="port-increment"),
            pytest.param(
                [_port(_ABS, 0.25), _port(_INC, 0.5)],
                1.75,
                id="port-absolute-increment",
            ),
            pytest.param(
                [_port(_INC, 0.5), _port(_ABS, 0.25)], 1.25, id="port-later-absolute"
            ),
            pytest.param(
                [_port(_ABS, 0.25), _port(_ABS, 0.75)], 1.75, id="port-two-absolutes"
            ),
            pytest.param([_device(_ABS, 0.125)], 0.625, id="device-absolute"),
            pytest.param([_device(_INC, 0.125)], 1.625, id="device-increment"),
            pytest.param(
                [_device(_ABS, 0.125, output=None)], 0.625, id="device-all-outputs"
            ),
            pytest.param(
                [_device(_ABS, 0.25), _device(_INC, 0.5)],
                1.25,
                id="device-absolute-increment",
            ),
        ],
    )
    def test_pin_delay_sets_or_adds_in_file_order(
        self, adds: list[Callable[[CellBuilder], CellBuilder]], expected: float
    ) -> None:
        cell = _wired_buffer(_builder())
        for add in adds:
            add(cell)
        assert _delay(TimingGraph(cell.build()), "src/Y", "u/Y") == expected

    def test_later_interconnect_replaces_an_earlier_port(self) -> None:
        builder = _builder()
        builder.add_cell("BUF", "u").add_iopath(input=_A, output=_Y, delay=_d(1.0))
        _port(_ABS, 0.25)(builder.add_cell("BUF", "u"))
        builder.add_cell("TOP", "").add_interconnect(
            source=PortSpec("src/Y"), load=PortSpec("u/A"), delay=_d(0.5)
        )
        assert _delay(TimingGraph(builder.build()), "src/Y", "u/Y") == 1.5

    def test_later_iopath_replaces_an_earlier_device(self) -> None:
        sdf = (
            _builder()
            .add_cell("BUF", "u")
            .add_device(output=_Y, delay=_d(0.25))
            .add_iopath(input=_A, output=_Y, delay=_d(1.0))
            .build()
        )
        assert _delay(TimingGraph(sdf), "u/A", "u/Y") == 1.0

    @pytest.mark.parametrize(
        "add",
        [
            pytest.param(
                lambda c: c.add_port(port=_A, delay=_d(0.25), cond=Cond("B")), id="port"
            ),
            pytest.param(
                lambda c: c.add_device(output=_Y, delay=_d(0.25), cond=Cond("B")),
                id="device",
            ),
        ],
    )
    def test_conditional_pin_delay_raises(
        self, add: Callable[[CellBuilder], CellBuilder]
    ) -> None:
        sdf = add(_wired_buffer(_builder())).build()
        with pytest.raises(ConditionalPinDelayError, match="COND"):
            TimingGraph(sdf)

    @pytest.mark.parametrize(
        ("mode", "expected"), [(_ABS, (0.5, 0.5)), (_INC, (1.5, 2.5))]
    )
    def test_device_without_output_covers_every_output(
        self, mode: DelayMode, expected: tuple[float, float]
    ) -> None:
        sdf = (
            _builder()
            .add_cell("DUAL", "u")
            .add_iopath(input=_A, output=_Y, delay=_d(1.0))
            .add_iopath(input=_A, output=_Z, delay=_d(2.0))
            .add_device(output=None, delay=_d(0.5), mode=mode)
            .build()
        )
        graph = TimingGraph(sdf)
        assert (_delay(graph, "u/A", "u/Y"), _delay(graph, "u/A", "u/Z")) == expected

    @pytest.mark.parametrize(
        "modes",
        [
            pytest.param([_ABS], id="absolute"),
            pytest.param([_ABS, _INC], id="absolute-increment"),
        ],
    )
    def test_port_on_an_undriven_pin_sits_before_the_path(
        self, modes: list[DelayMode]
    ) -> None:
        cell = _builder().add_cell("BUF", "u")
        for mode in modes:
            _port(mode, 0.25)(cell)
        cell.add_iopath(input=_A, output=_Y, delay=_d(1.0))
        assert _delay(TimingGraph(cell.build()), "u/A", "u/Y") == 1.0

    @pytest.mark.parametrize(
        ("add", "named"),
        [
            pytest.param(_port(_INC, 0.25), "PORT 'u/A'", id="port"),
            pytest.param(_device(_INC, 0.25), "DEVICE", id="device"),
        ],
    )
    def test_increment_before_any_arc_raises(
        self, add: Callable[[CellBuilder], CellBuilder], named: str
    ) -> None:
        cell = add(_builder().add_cell("BUF", "u"))
        cell.add_iopath(input=_A, output=_Y, delay=_d(1.0))
        with pytest.raises(IncrementWithoutBaseError, match=named):
            TimingGraph(cell.build())

    @pytest.mark.parametrize(
        ("output", "named"),
        [pytest.param(_Y, "'u/Y'", id="output"), pytest.param(None, "'u'", id="all")],
    )
    def test_device_without_iopath_raises(
        self, output: PortSpec | None, named: str
    ) -> None:
        sdf = _builder().add_cell("LUT", "u").add_device(output=output, delay=_d(1))
        with pytest.raises(UnplacedDeviceDelayError, match=named):
            TimingGraph(sdf.build())


_KINDS: dict[str, Callable[[CellBuilder, DelayMode, float], CellBuilder]] = {
    "iopath": lambda c, mode, v: c.add_iopath(
        input=_A, output=_Y, delay=_d(v), mode=mode
    ),
    "interconnect": lambda c, mode, v: c.add_interconnect(
        source=PortSpec("src/Y"), load=PortSpec("u/A"), delay=_d(v), mode=mode
    ),
    "port": lambda c, mode, v: c.add_port(port=_A, delay=_d(v), mode=mode),
}


def _increment_graph(kind: str, modes: list[tuple[DelayMode, float]]) -> TimingGraph:
    """The wired buffer with the *kind* arc built from *modes* entries in order."""
    builder = _builder()
    if kind != "interconnect":
        builder.add_cell("TOP", "").add_interconnect(
            source=PortSpec("src/Y"), load=PortSpec("u/A"), delay=_d(0.5)
        )
    if kind != "iopath":
        builder.add_cell("BUF", "u").add_iopath(input=_A, output=_Y, delay=_d(1.0))
    cell = (
        builder.add_cell("TOP", "")
        if kind == "interconnect"
        else builder.add_cell("BUF", "u")
    )
    for mode, value in modes:
        _KINDS[kind](cell, mode, value)
    return TimingGraph(builder.build())


class TestIncrement:
    @pytest.mark.parametrize(
        ("kind", "base_path"),
        [("iopath", 0.5), ("interconnect", 1.0), ("port", 1.0)],
    )
    @pytest.mark.parametrize(
        ("modes", "added"),
        [
            pytest.param([(_ABS, 2.0)], 2.0, id="absolute"),
            pytest.param([(_ABS, 2.0), (_INC, 0.25)], 2.25, id="increment"),
            pytest.param(
                [(_ABS, 2.0), (_INC, 0.25), (_INC, 0.5)], 2.75, id="two-increments"
            ),
            pytest.param([(_ABS, 2.0), (_INC, -0.5)], 1.5, id="negative-increment"),
        ],
    )
    def test_increment_adds_to_the_absolute_base(
        self,
        kind: str,
        base_path: float,
        modes: list[tuple[DelayMode, float]],
        added: float,
    ) -> None:
        graph = _increment_graph(kind, modes)
        assert _delay(graph, "src/Y", "u/Y") == base_path + added

    @pytest.mark.parametrize("kind", ["iopath", "interconnect"])
    @pytest.mark.parametrize(
        "modes",
        [
            pytest.param([(_INC, 0.25)], id="no-base"),
            pytest.param([(_INC, 0.25), (_ABS, 2.0)], id="base-after"),
        ],
    )
    def test_increment_without_base_raises(
        self, kind: str, modes: list[tuple[DelayMode, float]]
    ) -> None:
        with pytest.raises(IncrementWithoutBaseError, match="INCREMENT"):
            _increment_graph(kind, modes)

    def test_increment_matches_on_condition(self) -> None:
        sdf = (
            _builder()
            .add_cell("BUF", "u")
            .add_iopath(input=_A, output=_Y, delay=_d(1.0), cond=Cond("B"))
            .add_iopath(input=_A, output=_Y, delay=_d(0.5), mode=_INC, cond=Cond("!B"))
            .build()
        )
        with pytest.raises(IncrementWithoutBaseError, match="COND"):
            TimingGraph(sdf)


class TestEdgeAttributes:
    def test_each_edge_stores_one_timing_edge(self) -> None:
        sdf = _wired_buffer(_builder()).build()
        graph = TimingGraph(sdf)
        for u, v, attrs in graph.graph.edges(data=True):
            assert set(attrs) == {ARC_ATTR}
            arc = attrs[ARC_ATTR]
            assert isinstance(arc, TimingEdge)
            assert (arc.source, arc.sink) == (u, v)

    @pytest.mark.parametrize(
        ("setup", "expected"),
        [
            pytest.param(Values.uniform(0.07), 0.07, id="with-setup"),
            pytest.param(Values(), None, id="setup-omitted"),
        ],
    )
    def test_setuphold_edge_carries_setup_half(
        self, setup: Values, expected: float | None
    ) -> None:
        sdf = (
            _builder()
            .add_cell("DFF", "ff")
            .add_setuphold(
                data=TimingPortSpec("D"),
                reference=TimingPortSpec("CLK"),
                setup=setup,
                hold=Values.uniform(-0.03),
            )
            .build()
        )
        graph = TimingGraph(sdf)
        (edge,) = graph.edges()
        assert edge.entry_type == EntryType.SETUPHOLD
        cp = critical_path(graph, "ff/D", "ff/CLK")
        assert (None if cp is None else cp.scalar) == expected


def _setup(cell: CellBuilder, data: TimingPortSpec, limit: float) -> None:
    cell.add_setup(
        data=data, reference=TimingPortSpec("CLK"), limit=Values.uniform(limit)
    )


def _setuphold(cell: CellBuilder, data: TimingPortSpec, limit: float) -> None:
    cell.add_setuphold(
        data=data,
        reference=TimingPortSpec("CLK"),
        setup=Values.uniform(limit),
        hold=Values.uniform(0.0),
    )


class TestRegisterArcs:
    @pytest.mark.parametrize("add", [_setup, _setuphold], ids=["setup", "setuphold"])
    @pytest.mark.parametrize(
        ("checks", "expected"),
        [
            pytest.param(
                [("ff", "D", 1.0), ("ff", "D", 2.0)],
                [("ff/D", 2.0)],
                id="repeat-later-wins",
            ),
            pytest.param(
                [("ff", "D", 2.0), ("ff", "D", 1.0)],
                [("ff/D", 1.0)],
                id="repeat-later-wins-when-smaller",
            ),
            pytest.param(
                [("ff0", "D", 1.0), ("ff1", "D", 2.0)],
                [("ff0/D", 1.0), ("ff1/D", 2.0)],
                id="two-instances",
            ),
            pytest.param(
                [("ff", "D", 1.0), ("ff", "posedge D", 2.0)],
                [("ff/D", 1.0), ("ff/D", 2.0)],
                id="edge-variant",
            ),
        ],
    )
    def test_repeated_check_collapses_later_wins(
        self,
        add: Callable[[CellBuilder, TimingPortSpec, float], None],
        checks: list[tuple[str, str, float]],
        expected: list[tuple[str, float]],
    ) -> None:
        builder = _builder()
        for instance, data, limit in checks:
            edge, _, port = data.rpartition(" ")
            spec = TimingPortSpec(port, edge=EdgeType(edge) if edge else None)
            add(builder.add_cell("DFF", instance), spec, limit)
        arcs = sorted(
            (arc.source, arc.delay.rvalues[0].max)
            for arc in TimingGraph(builder.build()).edges()
        )
        assert arcs == expected


def _buffer_sdf(extra: str) -> str:
    return (
        '(DELAYFILE (DIVIDER /) (CELL (CELLTYPE "BUF") (INSTANCE u) '
        f"(DELAY (ABSOLUTE (IOPATH A Y (1)))) {extra}))"
    )


class TestEntriesWithoutArcs:
    @pytest.mark.parametrize(
        "extra",
        [
            pytest.param("(DELAY (PATHPULSE A Y (0.1) (0.2)))", id="pathpulse"),
            pytest.param("(DELAY (PATHPULSEPERCENT (10)))", id="pathpulsepercent"),
        ],
    )
    def test_entry_leaves_the_arcs_unchanged(self, extra: str) -> None:
        base = TimingGraph(parse_sdf(_buffer_sdf("")))
        assert TimingGraph(parse_sdf(_buffer_sdf(extra))).edges() == base.edges()

    def test_netdelay_raises(self) -> None:
        sdf = parse_sdf(_buffer_sdf("(DELAY (ABSOLUTE (NETDELAY n1 (1))))"))
        with pytest.raises(NetDelayError, match="NETDELAY n1"):
            TimingGraph(sdf)
