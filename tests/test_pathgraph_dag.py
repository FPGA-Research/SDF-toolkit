"""Longest-path analysis on chains, reconvergent ladders and loops."""

import inspect
import time
from collections.abc import Callable
from decimal import Decimal

import pytest

from sdf_toolkit.analysis.paths import (
    batch_endpoint_analysis,
    compute_slack,
    critical_path,
    rank_paths,
    verify_path,
)
from sdf_toolkit.core.builder import SDFBuilder
from sdf_toolkit.core.delays import PortSpec, TimingPortSpec
from sdf_toolkit.core.enums import Divider, EdgeSelect
from sdf_toolkit.core.pathgraph import TimingGraph, TimingLoopError
from sdf_toolkit.core.sdffile import SDFFile
from sdf_toolkit.core.values import DelayValues, Values

_A, _Y = PortSpec("A"), PortSpec("Y")


def _d(value: float) -> DelayValues:
    return DelayValues.of(Values.uniform(value))


def _builder() -> SDFBuilder:
    return SDFBuilder().set_header(divider=Divider.SLASH)


def _wire(builder: SDFBuilder, source: str, load: str, delay: float) -> None:
    builder.add_cell("TOP", "").add_interconnect(
        source=PortSpec(source), load=PortSpec(load), delay=_d(delay)
    )


def _buf(builder: SDFBuilder, instance: str, delay: float) -> None:
    builder.add_cell("BUF", instance).add_iopath(input=_A, output=_Y, delay=_d(delay))


def _chain(stages: int) -> SDFFile:
    """``in`` through *stages* buffers: one wire and one IOPATH per stage."""
    builder = _builder()
    previous = "in"
    for i in range(stages):
        _buf(builder, f"s{i}", 1.0)
        _wire(builder, previous, f"s{i}/A", 0.5)
        previous = f"s{i}/Y"
    return builder.build()


def _ladder(stages: int) -> SDFFile:
    """Two buffers per stage, every output wired to both next inputs.

    The ``a`` buffers are slower, so the critical path takes ``a`` at every
    stage and there are ``2**stages`` paths from ``in`` to ``out``.
    """
    builder = _builder()
    previous = ["in"]
    for i in range(stages):
        _buf(builder, f"a{i}", 2.0)
        _buf(builder, f"b{i}", 1.0)
        for source in previous:
            _wire(builder, source, f"a{i}/A", 0.1)
            _wire(builder, source, f"b{i}/A", 0.1)
        previous = [f"a{i}/Y", f"b{i}/Y"]
    for source in previous:
        _wire(builder, source, "out", 0.1)
    return builder.build()


def _ladder_critical(stages: int) -> Decimal:
    return stages * Decimal("2.1") + Decimal("0.1")


class TestLongChain:
    """Paths longer than any depth cutoff stay visible."""

    @pytest.mark.parametrize("stages", [1, 30, 60])
    def test_critical_path_covers_the_whole_chain(self, stages: int) -> None:
        graph = TimingGraph(_chain(stages))
        cp = critical_path(graph, "in", f"s{stages - 1}/Y")
        assert cp is not None
        assert len(cp.edges) == 2 * stages
        assert cp.scalar == pytest.approx(1.5 * stages)

    @pytest.mark.parametrize("stages", [1, 30, 60])
    def test_find_paths_returns_the_chain(self, stages: int) -> None:
        graph = TimingGraph(_chain(stages))
        (path,) = graph.find_paths("in", f"s{stages - 1}/Y")
        assert len(path) == 2 * stages

    @pytest.mark.parametrize("stages", [1, 30, 60])
    def test_batch_reports_the_chain(self, stages: int) -> None:
        (result,) = batch_endpoint_analysis(TimingGraph(_chain(stages)))
        assert (result.source, result.sink) == ("in", f"s{stages - 1}/Y")
        assert result.critical_delay == pytest.approx(1.5 * stages)
        assert result.path_count == 1


class TestReconvergentLadder:
    """Longest-path analysis is linear in the graph, not in the path count."""

    @pytest.mark.parametrize("stages", [1, 3, 16, 40])
    def test_critical_path_takes_the_slow_branch(self, stages: int) -> None:
        cp = critical_path(TimingGraph(_ladder(stages)), "in", "out")
        assert cp is not None
        assert cp.scalar == _ladder_critical(stages)
        assert [e.instance for e in cp.edges if e.instance] == [
            f"a{i}" for i in range(stages)
        ]

    @pytest.mark.parametrize("stages", [1, 3, 16, 40])
    def test_batch_counts_every_path(self, stages: int) -> None:
        (result,) = batch_endpoint_analysis(TimingGraph(_ladder(stages)))
        assert result.path_count == 2**stages
        assert result.critical_delay == _ladder_critical(stages)

    @pytest.mark.parametrize("stages", [1, 2, 4])
    def test_enumeration_agrees_with_longest_path(self, stages: int) -> None:
        graph = TimingGraph(_ladder(stages))
        ranked = rank_paths(graph, "in", "out")
        cp = critical_path(graph, "in", "out")
        assert cp is not None
        assert len(ranked) == 2**stages
        assert cp.scalar == ranked[0].scalar

    @pytest.mark.parametrize(
        "analysis",
        [
            pytest.param(lambda g: critical_path(g, "in", "out"), id="critical_path"),
            pytest.param(
                lambda g: compute_slack(g, "in", "out", period=Decimal(100)), id="slack"
            ),
            pytest.param(batch_endpoint_analysis, id="batch"),
        ],
    )
    def test_forty_stages_under_one_second(
        self, analysis: Callable[[TimingGraph], object]
    ) -> None:
        graph = TimingGraph(_ladder(40))
        start = time.perf_counter()
        analysis(graph)
        assert time.perf_counter() - start < 1.0


def _rise_fall(rise: float, fall: float) -> DelayValues:
    return DelayValues.of(Values.uniform(rise), Values.uniform(fall))


class TestTransitionSelection:
    """The longest path per transition equals the composed-delay ranking."""

    @pytest.fixture
    def crossed(self) -> TimingGraph:
        """Branch ``r`` is slow on rise, branch ``f`` is slow on fall."""
        builder = _builder()
        builder.add_cell("BUF", "r").add_iopath(
            input=_A, output=_Y, delay=_rise_fall(5.0, 1.0)
        )
        builder.add_cell("BUF", "f").add_iopath(
            input=_A, output=_Y, delay=_rise_fall(1.0, 4.0)
        )
        for inst in ("r", "f"):
            _wire(builder, "in", f"{inst}/A", 0.0)
            _wire(builder, f"{inst}/Y", "out", 0.0)
        return TimingGraph(builder.build())

    @pytest.mark.parametrize(
        ("edge", "instance", "expected"),
        [
            (EdgeSelect.RISE, "r", 5.0),
            (EdgeSelect.FALL, "f", 4.0),
            (EdgeSelect.WORST, "r", 5.0),
        ],
    )
    def test_selects_the_branch(
        self, crossed: TimingGraph, edge: EdgeSelect, instance: str, expected: float
    ) -> None:
        cp = critical_path(crossed, "in", "out", edge=edge)
        assert cp is not None
        assert cp.scalar == expected
        assert {e.instance for e in cp.edges} == {"", instance}
        assert rank_paths(crossed, "in", "out", edge=edge)[0].scalar == expected

    @pytest.mark.parametrize(
        "analysis",
        [
            pytest.param(
                lambda g: critical_path(g, "in", "out", edge=EdgeSelect.BEST),
                id="critical_path",
            ),
            pytest.param(
                lambda g: compute_slack(
                    g, "in", "out", period=1.0, edge=EdgeSelect.BEST
                ),
                id="slack",
            ),
            pytest.param(
                lambda g: batch_endpoint_analysis(g, edge=EdgeSelect.BEST), id="batch"
            ),
        ],
    )
    def test_best_has_no_longest_path_form(
        self, crossed: TimingGraph, analysis: Callable[[TimingGraph], object]
    ) -> None:
        with pytest.raises(ValueError, match="rank_paths"):
            analysis(crossed)


class TestTwelveRvalues:
    def test_worst_reads_the_x_transitions(self) -> None:
        rvalues = [Values.uniform(1.0)] * 12
        rvalues[6] = Values.uniform(9.0)  # 0x
        builder = _builder()
        builder.add_cell("BUF", "u").add_iopath(
            input=_A, output=_Y, delay=DelayValues(rvalues=tuple(rvalues))
        )
        graph = TimingGraph(builder.build())
        cp = critical_path(graph, "u/A", "u/Y")
        assert cp is not None
        assert cp.scalar == rank_paths(graph, "u/A", "u/Y")[0].scalar == 9.0


class TestBatchDisjointCones:
    def test_pairs_without_a_path_or_a_value(self) -> None:
        """Two separate buffers; ``n`` gives no max delay, so it sorts last."""
        builder = _builder()
        _buf(builder, "m", 2.0)
        builder.add_cell("BUF", "n").add_iopath(
            input=_A, output=_Y, delay=DelayValues.of(Values(min=1.0))
        )
        results = batch_endpoint_analysis(TimingGraph(builder.build()))
        assert [(r.source, r.sink, r.critical_delay) for r in results] == [
            ("m/A", "m/Y", 2.0),
            ("n/A", "n/Y", None),
        ]


def _loop(*, ends: bool) -> SDFFile:
    """``a -> b`` with ``b/Y`` fed back into ``a/A``; *ends* adds ``in`` and ``out``."""
    builder = _builder()
    _buf(builder, "a", 1.0)
    _buf(builder, "b", 1.0)
    _wire(builder, "a/Y", "b/A", 0.1)
    _wire(builder, "b/Y", "a/A", 0.1)
    if ends:
        _wire(builder, "in", "a/A", 0.1)
        _wire(builder, "b/Y", "out", 0.1)
    return builder.build()


def _register_feedback() -> SDFFile:
    """A flop whose Q drives its own D through a buffer."""
    builder = _builder()
    (
        builder.add_cell("DFF", "ff")
        .add_iopath(input=PortSpec("CLK"), output=PortSpec("Q"), delay=_d(0.3))
        .add_setup(
            data=TimingPortSpec("D"),
            reference=TimingPortSpec("CLK"),
            limit=Values.uniform(0.05),
        )
    )
    _buf(builder, "inc", 1.0)
    _wire(builder, "ff/Q", "inc/A", 0.1)
    _wire(builder, "inc/Y", "ff/D", 0.1)
    return builder.build()


def _critical_delay(graph: TimingGraph) -> Decimal | None:
    cp = critical_path(graph, "in", "out")
    return None if cp is None else cp.scalar


def _slack_delay(graph: TimingGraph) -> Decimal | None:
    slack = compute_slack(graph, "in", "out", period=10)
    return None if slack is None else 10 - slack


def _loop_beside_buffer() -> SDFFile:
    """The ``a -> b`` loop without ends, beside a buffer ``c`` from ``x``."""
    builder = _builder()
    _buf(builder, "a", 1.0)
    _buf(builder, "b", 1.0)
    _buf(builder, "c", 2.0)
    _wire(builder, "a/Y", "b/A", 0.1)
    _wire(builder, "b/Y", "a/A", 0.1)
    _wire(builder, "x", "c/A", 0.1)
    return builder.build()


_LOOP_PINS = {"a/A", "a/Y", "b/A", "b/Y"}


class TestTimingLoop:
    """A longest-path search raises on a cone with a loop and names one cycle."""

    @pytest.mark.parametrize(
        "analysis",
        [
            pytest.param(_critical_delay, id="critical_path"),
            pytest.param(_slack_delay, id="slack"),
            pytest.param(batch_endpoint_analysis, id="batch"),
        ],
    )
    def test_cyclic_cone_raises(
        self, analysis: Callable[[TimingGraph], object]
    ) -> None:
        graph = TimingGraph(_loop(ends=True))
        with pytest.raises(TimingLoopError) as info:
            analysis(graph)
        assert set(info.value.cycle) == _LOOP_PINS
        assert (info.value.source, info.value.sink) == ("in", "out")
        assert "in to out" in str(info.value)

    def test_ranking_still_enumerates_the_loop(self) -> None:
        (ranked,) = rank_paths(TimingGraph(_loop(ends=True)), "in", "out")
        assert ranked.scalar == Decimal("2.3")

    @pytest.mark.parametrize(
        ("sources", "sinks"),
        [
            pytest.param(None, None, id="defaults"),
            pytest.param(["a/A"], None, id="sinks-default"),
            pytest.param(None, ["b/Y"], id="sources-default"),
        ],
    )
    def test_batch_without_path_ends_raises_naming_the_cycle(
        self, sources: list[str] | None, sinks: list[str] | None
    ) -> None:
        graph = TimingGraph(_loop(ends=False))
        with pytest.raises(TimingLoopError) as info:
            batch_endpoint_analysis(graph, sources=sources, sinks=sinks)
        assert set(info.value.cycle) == _LOOP_PINS
        assert info.value.source is None
        assert "no startpoint or no endpoint" in str(info.value)

    def test_batch_searches_a_cone_beside_the_loop(self) -> None:
        (result,) = batch_endpoint_analysis(
            TimingGraph(_loop_beside_buffer()), sources=["x"], sinks=["c/Y"]
        )
        assert (result.critical_delay, result.path_count) == (Decimal("2.1"), 1)

    @pytest.mark.parametrize("traverse_registers", [True, False])
    def test_register_pins_are_path_ends(self, traverse_registers: bool) -> None:
        graph = TimingGraph(_register_feedback(), traverse_registers=traverse_registers)
        assert "ff/CLK" in graph.startpoints()
        assert "ff/D" in graph.endpoints()

    @pytest.mark.parametrize(
        ("traverse_registers", "raises"), [(True, True), (False, False)]
    )
    def test_register_feedback(self, traverse_registers: bool, raises: bool) -> None:
        graph = TimingGraph(_register_feedback(), traverse_registers=traverse_registers)
        if raises:
            with pytest.raises(TimingLoopError, match="traverse_registers"):
                batch_endpoint_analysis(graph)
        else:
            assert batch_endpoint_analysis(graph)


@pytest.mark.parametrize(
    "function", [critical_path, compute_slack, rank_paths, verify_path]
)
def test_options_after_graph_source_sink_are_keyword_only(
    function: Callable[..., object],
) -> None:
    params = list(inspect.signature(function).parameters.values())
    assert [p.name for p in params[:3]] == ["graph", "source", "sink"]
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params[3:])
