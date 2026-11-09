"""Register inputs, the combinational view and setup, hold and clock-to-output."""

from decimal import Decimal

import networkx as nx
import pytest
from conftest import DATA_DIR

from sdf_toolkit.analysis.paths import critical_path
from sdf_toolkit.core.enums import Corner
from sdf_toolkit.core.pathgraph import RegisterInput, RegisterInputTiming, TimingGraph
from sdf_toolkit.core.values import Values
from sdf_toolkit.parser.parser import parse_sdf, parse_sdf_file

_DFF_CHECKS = (
    "(SETUP D (posedge CLK) (0.1:0.2:0.3)) (HOLD D (posedge CLK) (0.01:0.02:0.03))"
)


def _dff(instance: str, *, clock_to_q: str = "(0.4:0.5:0.6) (0.7:0.8:0.9)") -> str:
    return _cell("DFF", instance, f"(IOPATH (posedge CLK) Q {clock_to_q})", _DFF_CHECKS)


def _cell(cell_type: str, instance: str, delays: str = "", checks: str = "") -> str:
    delay = f"(DELAY (ABSOLUTE {delays}))" if delays else ""
    check = f"(TIMINGCHECK {checks})" if checks else ""
    return f'(CELL (CELLTYPE "{cell_type}") (INSTANCE {instance}) {delay} {check})'


def _graph(*cells: str, traverse_registers: bool = False) -> TimingGraph:
    return TimingGraph(
        parse_sdf(f'(DELAYFILE (SDFVERSION "3.0") (DIVIDER /) {" ".join(cells)})'),
        traverse_registers=traverse_registers,
    )


def _top(interconnects: str) -> str:
    return _cell("TOP", "", interconnects)


def _limits(*texts: str) -> tuple[Values, ...]:
    return tuple(Values(min=Decimal(t), max=Decimal(t)) for t in texts)


_EDGE_VARIANTS = (
    "(HOLD (posedge D) (posedge CLK) (-0.028::-0.028))"
    " (HOLD (negedge D) (posedge CLK) (-0.045::-0.045))"
    " (SETUP (posedge D) (posedge CLK) (0.050::0.050))"
    " (SETUP (negedge D) (posedge CLK) (0.101::0.101))"
)


class TestRegisterInputs:
    @pytest.mark.parametrize("traverse_registers", [True, False])
    def test_gathers_every_check_of_a_pin_pair(self, traverse_registers: bool) -> None:
        graph = _graph(
            _cell("DFF", "ff", "(IOPATH CLK Q (1))", _EDGE_VARIANTS),
            traverse_registers=traverse_registers,
        )
        assert graph.register_inputs() == [
            RegisterInput(
                data="ff/D",
                clock="ff/CLK",
                setup=_limits("0.050", "0.101"),
                hold=_limits("-0.028", "-0.045"),
            )
        ]

    def test_setuphold_adds_both_limits_and_a_later_key_replaces(self) -> None:
        graph = _graph(
            _cell(
                "DFF",
                "ff",
                checks="(SETUPHOLD D (posedge CLK) (1) (2))"
                " (SETUP SE (posedge CLK) (3))"
                " (SETUP SE (posedge CLK) (4))"
                " (HOLD D (posedge CLK) (5))",
            )
        )
        assert graph.register_inputs() == [
            RegisterInput(
                data="ff/D",
                clock="ff/CLK",
                setup=(Values.uniform(1),),
                hold=(Values.uniform(2), Values.uniform(5)),
            ),
            RegisterInput(
                data="ff/SE", clock="ff/CLK", setup=(Values.uniform(4),), hold=()
            ),
        ]

    def test_within_keeps_the_registers_inside(self) -> None:
        graph = _graph(
            _top("(INTERCONNECT a/ff/Q b/ff/D (1))"),
            _dff("a/ff"),
            _dff("b/ff"),
        )
        assert [r.data for r in graph.within("b").register_inputs()] == ["b/ff/D"]
        assert [r.data for r in graph.register_inputs()] == ["a/ff/D", "b/ff/D"]


class TestCombinational:
    @pytest.mark.parametrize("traverse_registers", [True, False])
    def test_drops_every_arc_that_crosses_a_register(
        self, traverse_registers: bool
    ) -> None:
        graph = _graph(
            _top("(INTERCONNECT in ff/D (1)) (INTERCONNECT ff/Q buf/A (1))"),
            _dff("ff"),
            _cell("BUF", "buf", "(IOPATH A Y (1))"),
            _cell("LATCH", "hold_only", "(IOPATH G Q (1))", "(HOLD D (negedge G) (1))"),
            traverse_registers=traverse_registers,
        )
        comb = graph.combinational()
        assert [(a.source, a.sink) for a in comb.edges()] == [
            ("in", "ff/D"),
            ("ff/Q", "buf/A"),
            ("buf/A", "buf/Y"),
        ]
        assert comb.nodes() == graph.nodes()
        assert len(graph.edges()) == 5 + traverse_registers


class TestRegisterInputTiming:
    def test_adjusts_the_checks_for_data_and_clock_paths(self) -> None:
        graph = _graph(
            _top(
                "(INTERCONNECT DATA_IN U_FF/D (1.0:2.0:3.0) (1.5:2.5:4.0))"
                " (INTERCONNECT CLK_IN U_FF/CLK (0.2:0.3:0.4) (0.25:0.35:0.5))"
            ),
            _dff("U_FF"),
        )
        (register,) = graph.register_inputs()
        timing = graph.register_input_timing("DATA_IN", "CLK_IN", register)
        # Setup is late data 4.0 and limit 0.3 less early clock 0.2; hold is
        # limit 0.03 and late clock 0.5 less early data 1.0.
        assert timing == RegisterInputTiming(
            setup=Decimal("4.1"), hold=Decimal("-0.47")
        )

    def test_worst_edge_variant_counts_and_a_pin_is_its_own_arrival(self) -> None:
        graph = _graph(_cell("DFF", "ff", "(IOPATH CLK Q (1))", _EDGE_VARIANTS))
        (register,) = graph.register_inputs()
        assert graph.register_input_timing(
            "ff/D", "ff/CLK", register
        ) == RegisterInputTiming(setup=Decimal("0.101"), hold=Decimal("-0.028"))

    @pytest.mark.parametrize(
        ("late", "early", "expected"),
        [
            pytest.param(Corner.MAX, Corner.MIN, ("3.2", "-0.47"), id="max-min"),
            pytest.param(Corner.MIN, Corner.MIN, ("1.0", "-0.89"), id="min-min"),
            pytest.param(Corner.TYP, Corner.TYP, ("1.9", "-1.68"), id="typ-typ"),
        ],
    )
    def test_reads_late_and_early_corners(
        self, late: Corner, early: Corner, expected: tuple[str, str]
    ) -> None:
        graph = _graph(
            _top(
                "(INTERCONNECT d U_FF/D (1:2:3))"
                " (INTERCONNECT c U_FF/CLK (0.1:0.3:0.5))"
            ),
            _dff("U_FF"),
        )
        (register,) = graph.register_inputs()
        timing = graph.register_input_timing("d", "c", register, late=late, early=early)
        assert timing == RegisterInputTiming(
            setup=Decimal(expected[0]), hold=Decimal(expected[1])
        )

    def test_the_clock_passes_a_clock_gate(self) -> None:
        graph = _graph(
            _top("(INTERCONNECT c gate/CLK (1)) (INTERCONNECT gate/GCLK ff/CLK (1))"),
            _cell("ICG", "gate", "(IOPATH CLK GCLK (2))"),
            _dff("ff"),
        )
        (register,) = graph.register_inputs()
        timing = graph.register_input_timing("ff/D", "c", register)
        assert timing == RegisterInputTiming(
            setup=Decimal("-3.7"), hold=Decimal("4.03")
        )

    def test_data_does_not_cross_a_register(self) -> None:
        graph = _graph(
            _top(
                "(INTERCONNECT c a/CLK (0)) (INTERCONNECT c b/CLK (0))"
                " (INTERCONNECT a/Q b/D (1))"
            ),
            _dff("a"),
            _dff("b"),
        )
        register = graph.register_inputs()[1]
        assert register.data == "b/D"
        assert graph.register_input_timing("a/Q", "c", register) is not None
        assert graph.register_input_timing("a/CLK", "c", register) is None

    def test_the_clock_does_not_cross_a_setup_arc(self) -> None:
        graph = _graph(
            _top("(INTERCONNECT c ff/D (1))"), _dff("ff"), traverse_registers=True
        )
        (register,) = graph.register_inputs()
        assert nx.has_path(graph.graph, "c", "ff/CLK")
        assert graph.register_input_timing("c", "c", register) is None

    def test_a_missing_check_gives_none_for_its_kind(self) -> None:
        graph = _graph(_cell("DFF", "ff", checks="(SETUP D (posedge CLK) (1))"))
        (register,) = graph.register_inputs()
        assert graph.register_input_timing(
            "ff/D", "ff/CLK", register
        ) == RegisterInputTiming(setup=Decimal(1), hold=None)

    def test_rejects_an_unknown_pin(self) -> None:
        graph = _graph(_dff("ff"))
        (register,) = graph.register_inputs()
        with pytest.raises(nx.NodeNotFound, match="NOPE"):
            graph.register_input_timing("NOPE", "ff/CLK", register)


class TestClockToOutput:
    def test_joins_the_clock_path_and_the_register_output(self) -> None:
        graph = _graph(
            _top(
                "(INTERCONNECT CLK_IN U_FF/CLK (0.2:0.3:0.4) (0.25:0.35:0.5))"
                " (INTERCONNECT U_FF/Q OUT (0.1:0.2:0.3))"
            ),
            _dff("U_FF"),
        )
        path = critical_path(graph, "CLK_IN", "OUT", through="U_FF/CLK")
        assert path is not None
        # fall: 0.5 + 0.9 + 0.3, the larger of rise and fall
        assert path.scalar == Decimal("1.7")

    def test_each_register_takes_its_own_clock_path(self) -> None:
        registers = (("U1", "0.1", "1.0"), ("U2", "2.0", "4.0"))
        graph = _graph(
            _top(
                " ".join(
                    f"(INTERCONNECT BEL_CLK {name}/CLK ({clock}))"
                    f" (INTERCONNECT {name}/Q OUT (0.3))"
                    for name, clock, _ in registers
                )
            ),
            *(_dff(name, clock_to_q=f"({delay})") for name, _, delay in registers),
        )
        for name, clock, delay in registers:
            path = critical_path(graph, "BEL_CLK", "OUT", through=f"{name}/CLK")
            assert path is not None
            assert path.scalar == Decimal(clock) + Decimal(delay) + Decimal("0.3")


class TestSky130Cells:
    """OpenSTA timing of a FABulous logic cell and I/O buffer.

    The values are those FABulous characterises for the cells' BEL timing.
    """

    @pytest.fixture(scope="class")
    def graph(self) -> TimingGraph:
        sdf = parse_sdf_file(DATA_DIR / "sky130_lc_iobuf.sdf")
        return TimingGraph(sdf, traverse_registers=False)

    @staticmethod
    def _loads(graph: TimingGraph, port: str, view: TimingGraph) -> list[str]:
        return [arc.sink for arc in graph.successors(port) if arc.sink in view.graph]

    @staticmethod
    def _drivers(graph: TimingGraph, port: str, view: TimingGraph) -> list[str]:
        return [
            arc.source for arc in graph.predecessors(port) if arc.source in view.graph
        ]

    @pytest.mark.parametrize(
        ("instance", "port", "setup", "hold"),
        [
            ("Inst_LA_FABULOUS_LC", "I[0]", "1.86", "-0.779"),
            ("Inst_LA_FABULOUS_LC", "I[1]", "1.139", "-0.352"),
            ("Inst_LA_FABULOUS_LC", "I[2]", "0.859", "-0.251"),
            ("Inst_LA_FABULOUS_LC", "I[3]", "0.579", "-0.149"),
            ("Inst_LA_FABULOUS_LC", "Ci", "1.874", "-0.781"),
            ("Inst_LA_FABULOUS_LC", "SR", "0.322", "-0.031"),
            ("Inst_LA_FABULOUS_LC", "EN", "0.337", "-0.126"),
            ("Inst_A_IOBUF", "IO_IN", "0.101", "-0.028"),
            ("Inst_A_IOBUF", "IO_EN", "0.101", "-0.028"),
        ],
    )
    def test_setup_and_hold(
        self, graph: TimingGraph, instance: str, port: str, setup: str, hold: str
    ) -> None:
        view = graph.within(instance)
        timings = [
            timing
            for pin in self._loads(graph, port, view)
            for clock in self._loads(graph, "CLK", view)
            for register in view.register_inputs()
            if (timing := view.register_input_timing(pin, clock, register))
        ]
        assert max(t.setup for t in timings if t.setup is not None) == Decimal(setup)
        assert max(t.hold for t in timings if t.hold is not None) == Decimal(hold)

    @pytest.mark.parametrize(
        ("port", "output", "delay"),
        [
            ("I[0]", "O", "1.824"),
            ("I[1]", "O", "1.103"),
            ("I[1]", "Co", "0.292"),
            ("I[2]", "O", "0.823"),
            ("I[2]", "Co", "0.306"),
            ("I[3]", "O", "0.543"),
            ("Ci", "O", "1.838"),
            ("Ci", "Co", "0.284"),
        ],
    )
    def test_combinational_delay(
        self, graph: TimingGraph, port: str, output: str, delay: str
    ) -> None:
        view = graph.within("Inst_LA_FABULOUS_LC")
        comb = view.combinational()
        paths = [
            critical_path(comb, pin, out)
            for pin in self._loads(graph, port, view)
            for out in self._drivers(graph, output, view)
        ]
        assert max(p.scalar for p in paths if p is not None) == Decimal(delay)

    @pytest.mark.parametrize(
        ("instance", "output", "delay"),
        [("Inst_LA_FABULOUS_LC", "O", "0.631"), ("Inst_A_IOBUF", "IO_OUT", "0.52")],
    )
    def test_clock_to_output(
        self, graph: TimingGraph, instance: str, output: str, delay: str
    ) -> None:
        view = graph.within(instance)
        paths = [
            critical_path(view, clock, out, through=register_clock)
            for register_clock in {r.clock for r in view.register_inputs()}
            for clock in self._loads(graph, "CLK", view)
            for out in self._drivers(graph, output, view)
        ]
        assert max(p.scalar for p in paths if p is not None) == Decimal(delay)
