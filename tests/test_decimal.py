"""Delays are exact decimals from the parser to every sum and back to text."""

import random
from decimal import Decimal

import pytest
import typer
from typer.testing import CliRunner

from sdf_toolkit.cli import app
from sdf_toolkit.core.builder import SDFBuilder
from sdf_toolkit.core.model import (
    DelayValues,
    Divider,
    Iopath,
    PortSpec,
    Slack,
    Timescale,
    TimeUnit,
    Values,
    Waveform,
    WaveformEdge,
    format_number,
    to_decimal,
)
from sdf_toolkit.core.pathgraph import (
    TimingGraph,
    batch_endpoint_analysis,
    compute_slack,
    critical_path,
)
from sdf_toolkit.io.writer import emit_sdf
from sdf_toolkit.parser.parser import parse_sdf
from sdf_toolkit.transform.normalize import normalize_delays

runner = CliRunner()


def _sdf(body: str, *, timescale: str = "1ns") -> str:
    return (
        f'(DELAYFILE (SDFVERSION "3.0") (DIVIDER /) (TIMESCALE {timescale})'
        f' (CELL (CELLTYPE "BUF") (INSTANCE u) {body}))'
    )


def _first_iopath(text: str) -> Iopath:
    (entry,) = parse_sdf(text).cells["BUF"]["u"]
    assert isinstance(entry, Iopath)
    return entry


def _tenth_chain(stages: int) -> TimingGraph:
    """A chain whose every arc is 0.1, a value binary floats cannot hold."""
    builder = SDFBuilder().set_header(divider=Divider.SLASH)
    for i in range(stages):
        builder.add_cell("BUF", f"b{i}").add_iopath(
            input=PortSpec("A"),
            output=PortSpec("Y"),
            delay=DelayValues.of(Values.uniform(Decimal("0.1"))),
        )
    top = builder.add_cell("TOP", "")
    for i in range(stages - 1):
        top.add_interconnect(
            source=PortSpec(f"b{i}/Y"),
            load=PortSpec(f"b{i + 1}/A"),
            delay=DelayValues.of(Values.uniform(Decimal("0.2"))),
        )
    return TimingGraph(builder.build())


class TestToDecimal:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            pytest.param(0.1, "0.1", id="float-shortest-repr"),
            pytest.param(1e-5, "0.00001", id="float-exponent"),
            pytest.param(3, "3", id="int"),
            pytest.param(Decimal("0.050"), "0.050", id="decimal-kept"),
        ],
    )
    def test_converts_exactly(self, value: Decimal | float, expected: str) -> None:
        result = to_decimal(value)
        assert isinstance(result, Decimal)
        assert result == Decimal(expected)

    @pytest.mark.parametrize("value", [True, "0.1", None])
    def test_rejects_other_types(self, value: object) -> None:
        with pytest.raises(TypeError):
            to_decimal(value)  # ty: ignore[invalid-argument-type]

    @pytest.mark.parametrize(
        "make",
        [
            pytest.param(lambda: Values(min=0.1, typ=1, max=Decimal(2)).min, id="v"),
            pytest.param(lambda: Values.uniform(0.25).max, id="uniform"),
            pytest.param(
                lambda: (
                    Slack(
                        port="p",
                        rise_setup=Values(),
                        fall_setup=Values(),
                        rise_hold=Values(),
                        fall_hold=Values(),
                        period=0.5,  # ty: ignore[invalid-argument-type]
                    ).period
                ),
                id="slack-period",
            ),
            pytest.param(
                lambda: (
                    Waveform(
                        port="c",
                        period=10,  # ty: ignore[invalid-argument-type]
                        edges=(
                            WaveformEdge(edge="posedge", offsets=(0,)),  # ty: ignore[invalid-argument-type]
                            WaveformEdge(edge="negedge", offsets=(5,)),  # ty: ignore[invalid-argument-type]
                        ),
                    )
                    .edges[1]
                    .offsets[0]
                ),
                id="waveform-offset",
            ),
        ],
    )
    def test_constructors_store_decimals(self, make) -> None:
        assert isinstance(make(), Decimal)


class TestFormatNumber:
    """Written text matches what the float of the same value printed."""

    @pytest.mark.parametrize(
        "text",
        [
            "0",
            "-0",
            "1",
            "0.05",
            "0.050",
            "123.456",
            "1e-4",
            "1e-5",
            "0.00012",
            "1e15",
            "1e16",
            "1.5e16",
            "-2.75e-7",
            "100",
            "1e300",
        ],
    )
    def test_matches_float_repr(self, text: str) -> None:
        assert format_number(Decimal(text)) == repr(float(text))

    def test_matches_float_repr_for_random_values(self) -> None:
        rng = random.Random(1497)
        for _ in range(5000):
            value = rng.uniform(-1, 1) * 10 ** rng.randint(-20, 20)
            assert format_number(Decimal(repr(value))) == repr(value)

    def test_keeps_digits_a_float_drops(self) -> None:
        text = "0.12345678901234567890123"
        assert format_number(Decimal(text)) == text


class TestParsedValues:
    def test_parser_keeps_the_text_of_each_number(self) -> None:
        entry = _first_iopath(_sdf("(DELAY (ABSOLUTE (IOPATH A Y (0.050:1:2.10))))"))
        (rvalue,) = entry.delay.rvalues
        assert (str(rvalue.min), str(rvalue.typ), str(rvalue.max)) == (
            "0.050",
            "1",
            "2.10",
        )

    def test_writer_output_is_the_float_text(self) -> None:
        sdf = parse_sdf(_sdf("(DELAY (ABSOLUTE (IOPATH A Y (0.050:1:2.10))))"))
        assert "(IOPATH A Y (0.05:1.0:2.1))" in emit_sdf(sdf)

    def test_timescale_value_is_an_int(self) -> None:
        assert Timescale.parse("10.0 ps") == Timescale(value=10, unit=TimeUnit.PS)
        assert isinstance(Timescale.parse("100ns").value, int)


class TestExactSums:
    @pytest.mark.parametrize("stages", [1, 3, 10, 33])
    def test_critical_path_sums_exactly(self, stages: int) -> None:
        cp = critical_path(_tenth_chain(stages), "b0/A", f"b{stages - 1}/Y")
        assert cp is not None
        expected = stages * Decimal("0.1") + (stages - 1) * Decimal("0.2")
        assert cp.scalar == expected
        assert cp.delay == DelayValues.of(Values.uniform(expected))

    def test_slack_and_batch_are_exact(self) -> None:
        graph = _tenth_chain(3)
        assert compute_slack(graph, "b0/A", "b2/Y", period=1) == Decimal("0.3")
        (result,) = batch_endpoint_analysis(graph)
        assert result.critical_delay == Decimal("0.7")

    @pytest.mark.parametrize(
        ("source", "target", "value", "expected"),
        [
            pytest.param("1ps", "1ns", "0.1", "0.0001", id="ps-to-ns"),
            pytest.param("1ns", "1ps", "0.3", "300", id="ns-to-ps"),
            pytest.param("100ps", "10ns", "3", "0.03", id="hundreds"),
        ],
    )
    def test_normalize_is_exact(
        self, source: str, target: str, value: str, expected: str
    ) -> None:
        sdf = parse_sdf(
            _sdf(f"(DELAY (ABSOLUTE (IOPATH A Y ({value}))))", timescale=source)
        )
        result = normalize_delays(sdf, Timescale.parse(target))
        (entry,) = result.cells["BUF"]["u"]
        assert isinstance(entry, Iopath)
        assert entry.delay.rvalues[0].max == Decimal(expected)


class TestCli:
    def test_compose_prints_the_exact_sum(self, tmp_path) -> None:
        sdf_file = tmp_path / "chain.sdf"
        sdf_file.write_text(
            '(DELAYFILE (SDFVERSION "3.0") (DIVIDER /)'
            ' (CELL (CELLTYPE "BUF") (INSTANCE a) (DELAY (ABSOLUTE'
            " (IOPATH A Y (0.1)))))"
            ' (CELL (CELLTYPE "TOP") (INSTANCE) (DELAY (ABSOLUTE'
            " (INTERCONNECT a/Y b/A (0.2))))))"
        )
        result = runner.invoke(app, ["critical-path", str(sdf_file), "a/A", "b/A"])
        assert result.exit_code == 0, result.output
        assert "Critical path scalar (max, worst): 0.3" in result.output
        assert "0.30000000000000004" not in result.output

    @pytest.mark.parametrize("bad", ["abc", "nan", "-inf"])
    def test_rejects_a_number_option_that_is_not_finite(
        self, tmp_path, bad: str
    ) -> None:
        sdf_file = tmp_path / "a.sdf"
        sdf_file.write_text(_sdf("(DELAY (ABSOLUTE (IOPATH A Y (1))))"))
        result = runner.invoke(
            app, ["diff", str(sdf_file), str(sdf_file), f"--tolerance={bad}"]
        )
        assert result.exit_code == 2
        assert "Invalid value for '--tolerance'" in typer.unstyle(result.output)
