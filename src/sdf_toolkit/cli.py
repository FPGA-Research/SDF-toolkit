"""Typer CLI for parsing, emitting and analysing SDF timing files.

Exit codes follow diff(1). 0 means success, identical files or no lint
issue. 1 means a finding (differences, lint issues, a timing violation, a
failed verify, no path) or a data error in an input file. 2 means a usage
error, which Typer reports with the usage line.  Every library error derives
from {class}`SDFToolkitError`, and {class}`_Group` turns one raised by any
command into an ``Error:`` line and exit code 1.

stdout carries only the command result, so it can be piped. Text goes
through ``typer.echo`` and tables through the ``stdout`` rich console;
rich never prints JSON, SDF or DOT because it wraps lines at the console
width. stderr carries every status and error line through {func}`_status`
and {func}`_fail`.
"""

import json
from enum import StrEnum
from importlib.metadata import version
from pathlib import Path  # noqa: TC003 - required at runtime by Typer
from typing import TYPE_CHECKING, Annotated, NoReturn, assert_never

import typer
from rich.console import Console
from rich.table import Table
from typer.core import TyperGroup

from sdf_toolkit.analysis.diff import diff
from sdf_toolkit.analysis.export import to_dot
from sdf_toolkit.analysis.query import query
from sdf_toolkit.analysis.report import generate_report
from sdf_toolkit.analysis.stats import compute_stats, count_entry_types
from sdf_toolkit.analysis.validate import IssueSeverity, validate
from sdf_toolkit.core.model import (
    Corner,
    DelayValues,
    EdgeSelect,
    EntryType,
    MissingDelayError,
    SDFFile,
    Timescale,
    Values,
)
from sdf_toolkit.core.pathgraph import (
    TimingGraph,
    batch_endpoint_analysis,
    compute_slack,
    critical_path,
    decompose_delay,
    rank_paths,
    verify_path,
)
from sdf_toolkit.errors import SDFToolkitError
from sdf_toolkit.io.annotate import annotate_verilog, parse_yosys_json, run_yosys
from sdf_toolkit.io.transport import annotate_transport
from sdf_toolkit.io.writer import emit_sdf
from sdf_toolkit.parser.parser import parse_sdf
from sdf_toolkit.parser.standard import SDFStandard
from sdf_toolkit.transform.interconnect import port_to_interconnect
from sdf_toolkit.transform.merge import ConflictStrategy, merge
from sdf_toolkit.transform.normalize import normalize_delays

if TYPE_CHECKING:
    # Typer 0.26 and later vendor Click, so it is imported for the annotation only.
    import click

stdout = Console()


class OutputFormat(StrEnum):
    """Output format of the commands that write a whole SDF file."""

    json = "json"
    sdf = "sdf"


_INPUT_FILE_HELP = "Path to the SDF file."
_DELAY_JSON_EXAMPLE = '\'[{"min": 1.0, "typ": null, "max": 1.0}]\''

SdfPath = Annotated[
    Path,
    typer.Argument(exists=True, dir_okay=False, readable=True, help=_INPUT_FILE_HELP),
]
SourcePin = Annotated[str, typer.Argument(help="Source pin, e.g. P1/z.")]
SinkPin = Annotated[str, typer.Argument(help="Sink pin, e.g. P2/i.")]
OutputOption = Annotated[
    Path | None,
    typer.Option(
        "--output", "-o", dir_okay=False, help="Output file (default: stdout)."
    ),
]
FormatOption = Annotated[
    OutputFormat, typer.Option("--format", "-f", help="Output format.")
]
CornerOption = Annotated[
    Corner,
    typer.Option("--corner", case_sensitive=False, help="Process corner."),
]
StandardOption = Annotated[
    SDFStandard | None,
    typer.Option(
        "--standard",
        case_sensitive=False,
        help=(
            "Hold the SDF to the rules of this standard and reject an SDFVERSION "
            "that names the other. Without it the IEEE 1497 rules apply and the "
            "SDFVERSION is not checked."
        ),
    ),
]
LooseCondOption = Annotated[
    bool,
    typer.Option(
        "--loose-cond",
        help=(
            "Let a delay COND or CONDELSE wrap any number of entries of any delay "
            "type. Both standards allow one IOPATH."
        ),
    ),
]
EdgeOption = Annotated[
    EdgeSelect,
    typer.Option(
        "--edge",
        case_sensitive=False,
        help="Rvalue selection: the rise or fall transition, or the worst or best.",
    ),
]


def _status(message: str) -> None:
    """Write a status line to stderr."""
    typer.echo(message, err=True)


def _fail(message: str) -> NoReturn:
    """Write an error to stderr and exit with code 1."""
    typer.echo(f"Error: {message}", err=True)
    raise typer.Exit(code=1)


class _Group(TyperGroup):
    """Command group that reports any library error through {func}`_fail`."""

    def invoke(self, ctx: "click.Context") -> object:
        """Run the subcommand, turning an SDFToolkitError into exit code 1."""
        try:
            return super().invoke(ctx)
        except SDFToolkitError as exc:
            _fail(str(exc))


app = typer.Typer(cls=_Group, no_args_is_help=True)


def _write(text: str, *, output: Path | None) -> None:
    """Write *text* to *output*, or to stdout when *output* is None."""
    if output is None:
        typer.echo(text, nl=not text.endswith("\n"))
        return
    output.write_text(text)
    _status(f"Written to {output}")


def _render(sdf: SDFFile, fmt: OutputFormat) -> str:
    match fmt:
        case OutputFormat.json:
            return json.dumps(sdf.to_dict(), indent=2)
        case OutputFormat.sdf:
            return emit_sdf(sdf)
        case _:
            assert_never(fmt)


def _load_sdf(
    sdf_file: Path, *, standard: SDFStandard | None, loose_cond: bool
) -> SDFFile:
    return parse_sdf(sdf_file.read_text(), standard=standard, loose_cond=loose_cond)


def _require_pins(
    graph: TimingGraph,
    *,
    source: str,
    sink: str,
    hints: tuple[str, str] = ("SOURCE", "SINK"),
) -> None:
    """Raise a usage error naming the first pin that is not in *graph*."""
    nodes = graph.nodes()
    for pin, hint in zip((source, sink), hints, strict=True):
        if pin not in nodes:
            msg = f"pin {pin!r} is not in the timing graph"
            raise typer.BadParameter(msg, param_hint=hint)


def _path_graph(
    sdf_file: Path,
    *,
    source: str,
    sink: str,
    standard: SDFStandard | None,
    loose_cond: bool,
) -> TimingGraph:
    """Build the timing graph of *sdf_file* and check that both pins are in it."""
    graph = TimingGraph(_load_sdf(sdf_file, standard=standard, loose_cond=loose_cond))
    _require_pins(graph, source=source, sink=sink)
    return graph


def _timescale(text: str, *, param_hint: str) -> Timescale:
    """Parse a timescale option, reporting a malformed one as a usage error."""
    try:
        return Timescale.parse(text)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc


def _delay_from_json(json_str: str, *, param_hint: str) -> DelayValues:
    """Parse a JSON list of rvalues, each ``{"min": .., "typ": .., "max": ..}``.

    Raises
    ------
    typer.BadParameter
        If the JSON is not such a list, or has an rvalue count SDF does not
        define.
    """
    try:
        data = json.loads(json_str)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc
    if not isinstance(data, list):
        msg = f"expected a JSON list of rvalues, got {type(data).__name__}"
        raise typer.BadParameter(msg, param_hint=param_hint)
    try:
        return DelayValues(rvalues=tuple(Values(**item) for item in data))
    except (TypeError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint=param_hint) from exc


def _scalar_text(delay: DelayValues, *, corner: Corner, edge: EdgeSelect) -> str:
    """Format the delay's scalar, or ``None`` where the file gives no value."""
    try:
        return str(delay.scalar(corner=corner, edge=edge))
    except MissingDelayError:
        return "None"


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"sdf-toolkit {version('sdf-toolkit')}")
        raise typer.Exit


@app.callback()
def _root(
    show_version: Annotated[
        bool,
        typer.Option(
            "--version",
            callback=_show_version,
            is_eager=True,
            help="Show the version and exit.",
        ),
    ] = False,
) -> None:
    """Parse, emit and analyse OVI SDF 3.0 and IEEE 1497 (SDF 4.0) timing files."""


@app.command()
def parse(
    sdf_file: SdfPath,
    fmt: FormatOption = OutputFormat.json,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Parse an SDF file and output it as JSON or SDF."""
    _write(
        _render(_load_sdf(sdf_file, standard=standard, loose_cond=loose_cond), fmt),
        output=None,
    )


@app.command()
def emit(
    json_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="JSON file written by parse --format json.",
        ),
    ],
) -> None:
    """Convert a JSON file written by parse --format json back to SDF."""
    try:
        sdf = SDFFile.from_dict(json.loads(json_file.read_text()))
    except KeyError as exc:
        _fail(f"{json_file} has no {exc} key where parse --format json writes one")
    except (TypeError, ValueError) as exc:
        _fail(f"{json_file} is not SDF JSON: {exc}")
    _write(emit_sdf(sdf), output=None)


@app.command()
def info(
    sdf_file: SdfPath,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Show a summary of an SDF file: header, cell count and entry types."""
    sdf = _load_sdf(sdf_file, standard=standard, loose_cond=loose_cond)

    header_table = Table(title="SDF Header")
    header_table.add_column("Field", style="cyan")
    header_table.add_column("Value", style="green")
    for key, value in sdf.header.to_dict().items():
        header_table.add_row(str(key), value)
    stdout.print(header_table)

    instance_names = [name for instances in sdf.cells.values() for name in instances]

    summary_table = Table(title="Cell Summary")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="green")
    summary_table.add_row("Total cells", str(len(instance_names)))
    stdout.print(summary_table)

    type_table = Table(title="Entry Types")
    type_table.add_column("Type", style="cyan")
    type_table.add_column("Count", style="green")
    for entry_type, count in sorted(count_entry_types(sdf).items()):
        type_table.add_row(entry_type, str(count))
    stdout.print(type_table)

    instance_table = Table(title="Instances")
    instance_table.add_column("Instance", style="cyan")
    for inst in instance_names:
        instance_table.add_row(inst)
    stdout.print(instance_table)


@app.command()
def compose(
    sdf_file: SdfPath,
    source: SourcePin,
    sink: SinkPin,
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show full path details."),
    ] = False,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Compose delays along all paths from source to sink.

    Exits with 1 when no path joins them.
    """
    graph = _path_graph(
        sdf_file,
        source=source,
        sink=sink,
        standard=standard,
        loose_cond=loose_cond,
    )
    paths = graph.find_paths(source=source, sink=sink)
    if not paths:
        _fail(f"No path found from {source} to {sink}.")

    for i, path in enumerate(paths):
        composed = json.dumps(graph.compose_delay(path).to_list(), indent=2)
        if not verbose:
            typer.echo(f"Path {i + 1}: {composed}")
            continue
        typer.echo(f"Path {i + 1}:")
        for edge in path:
            typer.echo(
                f"  {edge.source} -> {edge.sink} ({edge.entry_type}, {edge.cell_type})"
            )
        typer.echo(f"  Composed: {composed}")


@app.command()
def verify(
    sdf_file: SdfPath,
    source: SourcePin,
    sink: SinkPin,
    expected: Annotated[
        str,
        typer.Option(
            "--expected",
            help=f"Expected delay as a JSON rvalue list, e.g. {_DELAY_JSON_EXAMPLE}",
        ),
    ],
    tolerance: Annotated[
        float,
        typer.Option("--tolerance", help="Absolute tolerance for comparison."),
    ] = 1e-9,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Verify that the composed path delay matches an expected value.

    Exits with 1 when any path differs from the expected delay.
    """
    expected_delay = _delay_from_json(expected, param_hint="--expected")
    graph = _path_graph(
        sdf_file,
        source=source,
        sink=sink,
        standard=standard,
        loose_cond=loose_cond,
    )
    result = verify_path(
        graph, source=source, sink=sink, expected=expected_delay, tolerance=tolerance
    )

    typer.echo("PASS" if result.passed else "FAIL")
    typer.echo(f"Expected: {json.dumps(expected_delay.to_list(), indent=2)}")
    for i, actual in enumerate(result.actual):
        typer.echo(f"Actual path {i + 1}: {json.dumps(actual.to_list(), indent=2)}")

    raise typer.Exit(code=0 if result.passed else 1)


@app.command()
def decompose(
    total: Annotated[
        str,
        typer.Option(
            "--total",
            help=f"Total delay as a JSON rvalue list, e.g. {_DELAY_JSON_EXAMPLE}",
        ),
    ],
    known: Annotated[
        str,
        typer.Option(
            "--known",
            help=f"Known delay as a JSON rvalue list, e.g. {_DELAY_JSON_EXAMPLE}",
        ),
    ],
) -> None:
    """Compute the unknown delay segment as total minus known."""
    total_delay = _delay_from_json(total, param_hint="--total")
    known_delay = _delay_from_json(known, param_hint="--known")
    try:
        result = decompose_delay(total=total_delay, known=known_delay)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint=["--total", "--known"]) from exc
    typer.echo(json.dumps(result.to_list(), indent=2))


@app.command(name="critical-path")
def critical_path_cmd(
    sdf_file: SdfPath,
    source: SourcePin,
    sink: SinkPin,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Find the critical (slowest) path from source to sink."""
    graph = _path_graph(
        sdf_file,
        source=source,
        sink=sink,
        standard=standard,
        loose_cond=loose_cond,
    )

    cp = critical_path(graph, source=source, sink=sink, corner=corner, edge=edge)
    if cp is None:
        _fail(f"No path found from {source} to {sink}.")

    typer.echo(f"Critical path scalar ({corner}, {edge}): {cp.scalar}")
    for timing_edge in cp.edges:
        scalar = _scalar_text(timing_edge.delay, corner=corner, edge=edge)
        typer.echo(f"  {timing_edge.source} -> {timing_edge.sink}  {scalar}")
    typer.echo(f"Delay: {json.dumps(cp.delay.to_list(), indent=2)}")


@app.command(name="rank-paths")
def rank_paths_cmd(
    sdf_file: SdfPath,
    source: SourcePin,
    sink: SinkPin,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    descending: Annotated[
        bool,
        typer.Option(
            "--descending/--ascending", help="Sort slowest first, or fastest first."
        ),
    ] = True,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of paths; 0 shows all."),
    ] = 0,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Rank all paths from source to sink by scalar delay."""
    graph = _path_graph(
        sdf_file,
        source=source,
        sink=sink,
        standard=standard,
        loose_cond=loose_cond,
    )

    ranked = rank_paths(
        graph,
        source=source,
        sink=sink,
        corner=corner,
        edge=edge,
        descending=descending,
    )
    if limit > 0:
        ranked = ranked[:limit]

    if not ranked:
        _fail(f"No paths found from {source} to {sink}.")

    for i, rp in enumerate(ranked):
        hops = " -> ".join([rp.edges[0].source] + [e.sink for e in rp.edges])
        typer.echo(f"#{i + 1}  scalar={rp.scalar}  {hops}")


@app.command()
def slack(
    sdf_file: SdfPath,
    source: SourcePin,
    sink: SinkPin,
    period: Annotated[
        float,
        typer.Argument(help="Clock period or timing constraint."),
    ],
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Compute the slack of the critical path as period minus its delay.

    Exits with 1 on a timing violation (negative slack).
    """
    graph = _path_graph(
        sdf_file,
        source=source,
        sink=sink,
        standard=standard,
        loose_cond=loose_cond,
    )

    result = compute_slack(
        graph, source=source, sink=sink, period=period, corner=corner, edge=edge
    )
    if result is None:
        _fail(f"No path from {source} to {sink} has a {corner} {edge} delay.")

    typer.echo(f"Slack: {result}")
    if result < 0:
        typer.echo("TIMING VIOLATION")
        raise typer.Exit(code=1)


@app.command()
def dot(
    sdf_file: SdfPath,
    output: OutputOption = None,
    highlight_source: Annotated[
        str | None,
        typer.Option(
            "--highlight-source",
            help="Source pin of the critical path to highlight.",
        ),
    ] = None,
    highlight_sink: Annotated[
        str | None,
        typer.Option(
            "--highlight-sink",
            help="Sink pin of the critical path to highlight.",
        ),
    ] = None,
    cluster: Annotated[
        bool,
        typer.Option("--cluster/--no-cluster", help="Cluster nodes by instance."),
    ] = False,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Export the timing graph in DOT (Graphviz) format.

    --highlight-source and --highlight-sink go together; the pair exits with 1
    when no path joins them.
    """
    graph = TimingGraph(_load_sdf(sdf_file, standard=standard, loose_cond=loose_cond))

    highlight = None
    match (highlight_source, highlight_sink):
        case (None, None):
            pass
        case (str() as src, str() as snk):
            _require_pins(
                graph,
                source=src,
                sink=snk,
                hints=("--highlight-source", "--highlight-sink"),
            )
            highlight = critical_path(
                graph, source=src, sink=snk, corner=corner, edge=edge
            )
            if highlight is None:
                _fail(f"No path from {src} to {snk} to highlight.")
        case _:
            msg = "pass --highlight-source and --highlight-sink together"
            raise typer.BadParameter(
                msg, param_hint=["--highlight-source", "--highlight-sink"]
            )

    dot_str = to_dot(
        graph,
        highlight_path=highlight,
        cluster_by_instance=cluster,
        corner=corner,
        edge=edge,
    )
    _write(dot_str, output=output)


@app.command()
def annotate(
    sdf_file: SdfPath,
    verilog_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Verilog file that defines a module for every SDF CELLTYPE.",
        ),
    ],
    output: OutputOption = None,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Annotate a Verilog file with SDF specify blocks and net delays."""
    result = annotate_verilog(
        sdf_path=sdf_file,
        verilog_path=verilog_file,
        corner=corner,
        edge=edge,
        standard=standard,
        loose_cond=loose_cond,
    )
    _write(result, output=output)


@app.command(name="annotate-transport")
def annotate_transport_cmd(
    sdf_file: SdfPath,
    verilog_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Flat gate-level Verilog netlist.",
        ),
    ],
    top: Annotated[str, typer.Option("--top", help="Top module of the netlist.")],
    library: Annotated[
        list[Path] | None,
        typer.Option(
            "--library",
            "-l",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Port-only cell views, read as blackboxes. Repeat for each file.",
        ),
    ] = None,
    output: OutputOption = None,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Annotate a flat netlist with per-sink transport delay buffers."""
    result = annotate_transport(
        sdf_path=sdf_file,
        verilog_path=verilog_file,
        top=top,
        library=library or (),
        corner=corner,
        edge=edge,
        standard=standard,
        loose_cond=loose_cond,
    )
    _write(result, output=output)


@app.command(name="port-to-interconnect")
def port_to_interconnect_cmd(
    sdf_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Path to the SDF file containing PORT delays.",
        ),
    ],
    verilog_file: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Path to the gate-level Verilog netlist.",
        ),
    ],
    output: OutputOption = None,
    top_module: Annotated[
        str | None,
        typer.Option("--top", help="Top module name (default: SDF DESIGN)."),
    ] = None,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Rewrite PORT (sink-only) delays as INTERCONNECT (source to sink) delays.

    Yosys reads the Verilog netlist to find the driver of each PORT load.
    Exits with 1 when a load has no driver or more than one.
    """
    sdf = _load_sdf(sdf_file, standard=standard, loose_cond=loose_cond)
    design = parse_yosys_json(run_yosys(verilog_file))
    result = port_to_interconnect(sdf, design, top_module=top_module)
    _write(emit_sdf(result), output=output)


@app.command()
def normalize(
    sdf_file: SdfPath,
    target: Annotated[
        str,
        typer.Option("--target", help="Target timescale, e.g. 1ns or 1ps."),
    ],
    fmt: FormatOption = OutputFormat.json,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Normalize all delays in an SDF file to a target timescale."""
    target_timescale = _timescale(target, param_hint="--target")
    result = normalize_delays(
        _load_sdf(sdf_file, standard=standard, loose_cond=loose_cond), target_timescale
    )
    _write(_render(result, fmt), output=None)


@app.command()
def lint(
    sdf_file: SdfPath,
    severity: Annotated[
        IssueSeverity | None,
        typer.Option(
            "--severity",
            case_sensitive=False,
            help="Report only issues of this severity (default: all).",
        ),
    ] = None,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Validate an SDF file and report structural and semantic issues.

    Exits with 1 when it reports any issue.
    """
    issues = validate(_load_sdf(sdf_file, standard=standard, loose_cond=loose_cond))
    if severity is not None:
        issues = [i for i in issues if i.severity == severity]

    if not issues:
        _status("No issues found.")
        return

    table = Table(title="Lint Issues")
    table.add_column("Severity", style="red")
    table.add_column("Cell Type", style="cyan")
    table.add_column("Instance", style="cyan")
    table.add_column("Entry", style="cyan")
    table.add_column("Message", style="yellow")
    for issue in issues:
        table.add_row(
            issue.severity,
            issue.cell_type or "-",
            issue.instance or "-",
            issue.entry_name or "-",
            issue.message,
        )
    stdout.print(table)
    raise typer.Exit(code=1)


@app.command()
def stats(
    sdf_file: SdfPath,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Compute aggregate statistics over delay values."""
    result = compute_stats(
        _load_sdf(sdf_file, standard=standard, loose_cond=loose_cond),
        corner=corner,
        edge=edge,
    )

    table = Table(title="SDF Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")
    table.add_row("Total cell types", str(result.total_cells))
    table.add_row("Total instances", str(result.total_instances))
    table.add_row("Total entries", str(result.total_entries))
    table.add_row("Delays without value", str(result.missing_delay_count))
    table.add_row("Delay min", str(result.delay_min))
    table.add_row("Delay max", str(result.delay_max))
    table.add_row("Delay mean", str(result.delay_mean))
    table.add_row("Delay median", str(result.delay_median))
    stdout.print(table)

    if result.entry_type_counts:
        type_table = Table(title="Entry Type Counts")
        type_table.add_column("Type", style="cyan")
        type_table.add_column("Count", style="green")
        for etype, count in sorted(result.entry_type_counts.items()):
            type_table.add_row(etype, str(count))
        stdout.print(type_table)


@app.command(name="query")
def query_cmd(
    sdf_file: SdfPath,
    cell_type: Annotated[
        list[str] | None,
        typer.Option("--cell-type", help="Filter by cell type (repeatable)."),
    ] = None,
    instance: Annotated[
        list[str] | None,
        typer.Option("--instance", help="Filter by instance name (repeatable)."),
    ] = None,
    entry_type: Annotated[
        list[EntryType] | None,
        typer.Option(
            "--entry-type",
            case_sensitive=False,
            help="Filter by entry type (repeatable).",
        ),
    ] = None,
    pin_pattern: Annotated[
        str | None,
        typer.Option("--pin-pattern", help="Regex searched in every entry port."),
    ] = None,
    min_delay: Annotated[
        float | None,
        typer.Option("--min-delay", help="Minimum delay threshold."),
    ] = None,
    max_delay: Annotated[
        float | None,
        typer.Option("--max-delay", help="Maximum delay threshold."),
    ] = None,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    fmt: FormatOption = OutputFormat.json,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Filter and query SDF file entries."""
    result = query(
        _load_sdf(sdf_file, standard=standard, loose_cond=loose_cond),
        cell_types=cell_type,
        instances=instance,
        entry_types=entry_type,
        pin_pattern=pin_pattern,
        min_delay=min_delay,
        max_delay=max_delay,
        corner=corner,
        edge=edge,
    )
    _write(_render(result, fmt), output=None)


@app.command(name="diff")
def diff_cmd(
    file_a: SdfPath,
    file_b: SdfPath,
    tolerance: Annotated[
        float,
        typer.Option("--tolerance", help="Absolute tolerance for value comparison."),
    ] = 1e-9,
    normalize_first: Annotated[
        bool,
        typer.Option(
            "--normalize/--no-normalize", help="Normalize timescales before comparing."
        ),
    ] = False,
    target_timescale: Annotated[
        str,
        typer.Option("--target-timescale", help="Target timescale for normalization."),
    ] = "1ps",
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Compare two SDF files and report differences.

    Exits with 1 when the files differ.
    """
    target = _timescale(target_timescale, param_hint="--target-timescale")
    result = diff(
        a=_load_sdf(file_a, standard=standard, loose_cond=loose_cond),
        b=_load_sdf(file_b, standard=standard, loose_cond=loose_cond),
        tolerance=tolerance,
        normalize_first=normalize_first,
        target_timescale=target,
    )

    if result.header_diffs:
        header_table = Table(title="Header Differences")
        header_table.add_column("Field", style="cyan")
        header_table.add_column("File A", style="green")
        header_table.add_column("File B", style="yellow")
        for fld, (va, vb) in result.header_diffs.items():
            header_table.add_row(str(fld), str(va), str(vb))
        stdout.print(header_table)

    if result.only_in_a:
        typer.echo(f"\nOnly in A: {len(result.only_in_a)} entries")
        for location in result.only_in_a[:20]:
            typer.echo(f"  {location}")

    if result.only_in_b:
        typer.echo(f"\nOnly in B: {len(result.only_in_b)} entries")
        for location in result.only_in_b[:20]:
            typer.echo(f"  {location}")

    if result.value_diffs:
        diff_table = Table(title=f"Value Differences ({len(result.value_diffs)} total)")
        diff_table.add_column("Cell/Instance/Arc", style="cyan")
        diff_table.add_column("Field", style="cyan")
        diff_table.add_column("A", style="green")
        diff_table.add_column("B", style="yellow")
        diff_table.add_column("Delta", style="red")
        for d in result.value_diffs[:50]:
            # A repeated arc carries its occurrence so the rows stay distinct.
            repeat = f" #{d.occurrence}" if d.occurrence else ""
            diff_table.add_row(
                f"{d.cell_type}/{d.instance}/{d.key}{repeat}",
                str(d.field),
                str(d.value_a),
                str(d.value_b),
                str(d.delta),
            )
        stdout.print(diff_table)

    if (
        result.header_diffs
        or result.only_in_a
        or result.only_in_b
        or result.value_diffs
    ):
        raise typer.Exit(code=1)
    _status("Files are identical.")


@app.command(name="merge")
def merge_cmd(
    files: Annotated[
        list[Path],
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="SDF files to merge (at least 2).",
        ),
    ],
    strategy: Annotated[
        ConflictStrategy,
        typer.Option(
            "--strategy",
            case_sensitive=False,
            help="What to do when two files time the same arc.",
        ),
    ] = ConflictStrategy.ERROR,
    target_timescale: Annotated[
        str | None,
        typer.Option(
            "--target-timescale",
            help="Normalize to this timescale before merging.",
        ),
    ] = None,
    fmt: FormatOption = OutputFormat.json,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Merge two or more SDF files into one."""
    if len(files) < 2:
        msg = f"merge needs at least 2 files, got {len(files)}"
        raise typer.BadParameter(msg, param_hint="FILES")
    target = (
        None
        if target_timescale is None
        else _timescale(target_timescale, param_hint="--target-timescale")
    )
    result = merge(
        [_load_sdf(f, standard=standard, loose_cond=loose_cond) for f in files],
        strategy=strategy,
        target_timescale=target,
    )
    _write(_render(result, fmt), output=None)


@app.command(name="batch-analysis")
def batch_analysis_cmd(
    sdf_file: SdfPath,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of results; 0 shows all."),
    ] = 20,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Analyse every startpoint to endpoint pair in the timing graph."""
    graph = TimingGraph(_load_sdf(sdf_file, standard=standard, loose_cond=loose_cond))

    results = batch_endpoint_analysis(graph, corner=corner, edge=edge)
    if limit > 0:
        results = results[:limit]

    if not results:
        _fail("No endpoint pairs found.")

    table = Table(title="Batch Endpoint Analysis")
    table.add_column("Rank", style="cyan")
    table.add_column("Source", style="green")
    table.add_column("Sink", style="green")
    table.add_column("Critical Delay", style="yellow")
    table.add_column("Path Count", style="cyan")
    for i, r in enumerate(results):
        table.add_row(
            str(i + 1),
            r.source,
            r.sink,
            str(r.critical_delay),
            str(r.path_count),
        )
    stdout.print(table)


@app.command()
def report(
    sdf_file: SdfPath,
    corner: CornerOption = Corner.MAX,
    edge: EdgeOption = EdgeSelect.WORST,
    top_n: Annotated[
        int,
        typer.Option("--top-n", help="Number of top critical paths to show."),
    ] = 10,
    period: Annotated[
        float | None,
        typer.Option("--period", help="Clock period for slack analysis."),
    ] = None,
    standard: StandardOption = None,
    loose_cond: LooseCondOption = False,
) -> None:
    """Generate a timing report of the critical paths and slack."""
    text = generate_report(
        _load_sdf(sdf_file, standard=standard, loose_cond=loose_cond),
        corner=corner,
        edge=edge,
        top_n_paths=top_n,
        period=period,
    )
    _write(text, output=None)


def main() -> None:
    """Entry point for the SDF timing CLI."""
    app()
