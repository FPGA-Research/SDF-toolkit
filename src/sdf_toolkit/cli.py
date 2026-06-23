"""Typer CLI for parsing, emitting, and analyzing SDF timing files.

Provides commands to parse SDF files to JSON, emit JSON back to SDF,
display file summaries, compose path delays, verify expected delays,
and decompose delay segments.
"""

import json
from collections import Counter
from enum import StrEnum
from pathlib import Path  # noqa: TC003 - required at runtime by Typer
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from sdf_toolkit.analysis.export import to_dot
from sdf_toolkit.core.model import (
    BaseEntry,
    DelayPaths,
    EntryType,
    SDFFile,
    SDFHeader,
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
from sdf_toolkit.io.sdfparse import emit as sdf_emit
from sdf_toolkit.parser.parser import parse_sdf

app = typer.Typer(no_args_is_help=True)
console = Console()


def _load_sdf(sdf_file: Path) -> SDFFile:
    """Parse an SDF file and return the SDFFile object.

    Parameters
    ----------
    sdf_file : Path
        Path to the SDF file.

    Returns
    -------
    SDFFile
        The parsed SDF file.
    """
    return parse_sdf(sdf_file.read_text())


def _load_graph(sdf_file: Path) -> tuple[SDFFile, TimingGraph]:
    """Parse an SDF file and return the SDFFile and its TimingGraph.

    Parameters
    ----------
    sdf_file : Path
        Path to the SDF file.

    Returns
    -------
    tuple[SDFFile, TimingGraph]
        The parsed SDF file and its timing graph.
    """
    sdf = _load_sdf(sdf_file)
    return sdf, TimingGraph(sdf)


class OutputFormat(StrEnum):
    """Output format for the parse command."""

    json = "json"
    sdf = "sdf"


def _sdffile_from_dict(data: dict[str, object]) -> SDFFile:
    """Reconstruct an SDFFile from a dictionary produced by ``SDFFile.to_dict``.

    Parameters
    ----------
    data : dict[str, object]
        The dictionary representation of an SDF file.

    Returns
    -------
    SDFFile
        The reconstructed SDFFile object.
    """
    header = SDFHeader(**data.get("header", {}))  # type: ignore[arg-type]
    cells: dict[str, dict[str, dict[str, BaseEntry]]] = {}
    for cell_type, instances in data.get("cells", {}).items():  # type: ignore[union-attr]
        cells[cell_type] = {}
        for instance, entries in instances.items():
            cells[cell_type][instance] = {}
            for name, entry_dict in entries.items():
                fields = dict(entry_dict)
                # Convert delay_paths back to DelayPaths
                dp_dict = fields.pop("delay_paths", None)
                delay_paths = None
                if dp_dict is not None:
                    delay_paths = DelayPaths(
                        **{
                            k: Values(**v) if v is not None else None
                            for k, v in dp_dict.items()
                        }
                    )
                # Convert type string back to EntryType
                entry_type = fields.pop("type", "iopath")
                entry = BaseEntry(
                    **fields,
                    type=EntryType(entry_type),
                    delay_paths=delay_paths,
                )
                cells[cell_type][instance][name] = entry
    return SDFFile(header=header, cells=cells)


def _delay_paths_from_json(json_str: str) -> DelayPaths:
    """Parse a JSON string into a DelayPaths object.

    Parameters
    ----------
    json_str : str
        JSON string representing delay path values, e.g.
        ``'{"nominal": {"min": 1.0, "avg": null, "max": 1.0}}'``.

    Returns
    -------
    DelayPaths
        The reconstructed DelayPaths object.
    """
    data: dict[str, dict[str, float | None] | None] = json.loads(json_str)
    return DelayPaths(
        **{k: Values(**v) if v is not None else None for k, v in data.items()}
    )


@app.command()
def parse(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file to parse."),
    ],
    fmt: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format."),
    ] = OutputFormat.json,
    timescale: Annotated[
        str,
        typer.Option("--timescale", "-t", help="Timescale for SDF output."),
    ] = "1ps",
) -> None:
    """Parse an SDF file and output as JSON or SDF."""
    sdf = _load_sdf(sdf_file)

    if fmt == OutputFormat.json:
        typer.echo(json.dumps(sdf.to_dict(), indent=2))
    else:
        typer.echo(sdf_emit(sdf, timescale=timescale))


@app.command()
def emit(
    json_file: Annotated[
        Path,
        typer.Argument(help="Path to the JSON file to convert."),
    ],
    timescale: Annotated[
        str,
        typer.Option("--timescale", "-t", help="Timescale for SDF output."),
    ] = "1ps",
) -> None:
    """Convert a JSON file (produced by ``parse --format json``) back to SDF."""
    data = json.loads(json_file.read_text())
    sdf = _sdffile_from_dict(data)
    typer.echo(sdf_emit(sdf, timescale=timescale))


@app.command()
def info(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file to inspect."),
    ],
) -> None:
    """Show a summary of an SDF file (header, cell count, entry types)."""
    sdf = _load_sdf(sdf_file)

    # Header table
    header_table = Table(title="SDF Header")
    header_table.add_column("Field", style="cyan")
    header_table.add_column("Value", style="green")
    for key, value in sdf.header.items():
        header_table.add_row(key, str(value))
    console.print(header_table)

    # Cell summary
    instance_names: list[str] = []
    entry_type_counts: Counter[str] = Counter()

    for cell_instances in sdf.cells.values():
        for instance_name, entries in cell_instances.items():
            instance_names.append(instance_name)
            for entry in entries.values():
                entry_type_counts[str(entry.type)] += 1

    summary_table = Table(title="Cell Summary")
    summary_table.add_column("Metric", style="cyan")
    summary_table.add_column("Value", style="green")
    summary_table.add_row("Total cells", str(len(instance_names)))
    console.print(summary_table)

    # Entry type breakdown
    type_table = Table(title="Entry Types")
    type_table.add_column("Type", style="cyan")
    type_table.add_column("Count", style="green")
    for entry_type, count in sorted(entry_type_counts.items()):
        type_table.add_row(entry_type, str(count))
    console.print(type_table)

    # Instance list
    instance_table = Table(title="Instances")
    instance_table.add_column("Instance", style="cyan")
    for inst in instance_names:
        instance_table.add_row(inst)
    console.print(instance_table)


@app.command()
def compose(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    source: Annotated[
        str,
        typer.Argument(help="Source pin name."),
    ],
    sink: Annotated[
        str,
        typer.Argument(help="Sink pin name."),
    ],
    verbose: Annotated[
        bool,
        typer.Option("--verbose", "-v", help="Show full path details."),
    ] = False,
) -> None:
    """Compose delays along all paths from source to sink."""
    _sdf, graph = _load_graph(sdf_file)

    if verbose:
        paths = graph.find_paths(source, sink)
        for i, path in enumerate(paths):
            typer.echo(f"Path {i + 1}:")
            for edge in path:
                typer.echo(
                    f"  {edge.source} -> {edge.sink}"
                    f" ({edge.entry_type}, {edge.cell_type})"
                )
            composed = graph.compose_delay(path)
            typer.echo(f"  Composed: {json.dumps(composed.to_dict(), indent=2)}")
    else:
        delays = graph.compose(source, sink)
        for i, delay in enumerate(delays):
            typer.echo(f"Path {i + 1}: {json.dumps(delay.to_dict(), indent=2)}")


@app.command()
def verify(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    source: Annotated[
        str,
        typer.Argument(help="Source pin name."),
    ],
    sink: Annotated[
        str,
        typer.Argument(help="Sink pin name."),
    ],
    expected: Annotated[
        str,
        typer.Option(
            "--expected",
            help=(
                "Expected delay as a JSON string, e.g. "
                '\'{"nominal": {"min": 1.0, "avg": null, "max": 1.0}}\''
            ),
        ),
    ],
    tolerance: Annotated[
        float,
        typer.Option("--tolerance", help="Absolute tolerance for comparison."),
    ] = 1e-9,
) -> None:
    """Verify that composed path delay matches an expected value."""
    _sdf, graph = _load_graph(sdf_file)

    expected_delay = _delay_paths_from_json(expected)
    result = verify_path(graph, source, sink, expected_delay, tolerance=tolerance)

    if result.passed:
        typer.echo("PASS")
    else:
        typer.echo("FAIL")

    typer.echo(f"Expected: {json.dumps(expected_delay.to_dict(), indent=2)}")
    for i, actual in enumerate(result.actual):
        typer.echo(f"Actual path {i + 1}: {json.dumps(actual.to_dict(), indent=2)}")

    raise typer.Exit(code=0 if result.passed else 1)


@app.command()
def decompose(
    total: Annotated[
        str,
        typer.Option(
            "--total",
            help=(
                "Total delay as a JSON string, e.g. "
                '\'{"nominal": {"min": 2.0, "avg": null, "max": 2.0}}\''
            ),
        ),
    ],
    known: Annotated[
        str,
        typer.Option(
            "--known",
            help=(
                "Known delay segment as a JSON string, e.g. "
                '\'{"nominal": {"min": 1.0, "avg": null, "max": 1.0}}\''
            ),
        ),
    ],
) -> None:
    """Compute the unknown delay segment from total and known delays."""
    total_delay = _delay_paths_from_json(total)
    known_delay = _delay_paths_from_json(known)
    result = decompose_delay(total_delay, known_delay)
    typer.echo(json.dumps(result.to_dict(), indent=2))


@app.command(name="critical-path")
def critical_path_cmd(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    source: Annotated[
        str,
        typer.Argument(help="Source pin name."),
    ],
    sink: Annotated[
        str,
        typer.Argument(help="Sink pin name."),
    ],
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, …)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
) -> None:
    """Find the critical (slowest) path from source to sink."""
    _sdf, graph = _load_graph(sdf_file)

    cp = critical_path(graph, source, sink, field, metric)
    if cp is None:
        typer.echo("No path found.")
        raise typer.Exit(code=1)

    typer.echo(f"Critical path scalar ({field}.{metric}): {cp.scalar}")
    for edge in cp.edges:
        scalar = edge.delay.get_scalar(field, metric)
        typer.echo(f"  {edge.source} -> {edge.sink}  {scalar}")
    typer.echo(f"Delay: {json.dumps(cp.delay.to_dict(), indent=2)}")


@app.command(name="rank-paths")
def rank_paths_cmd(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    source: Annotated[
        str,
        typer.Argument(help="Source pin name."),
    ],
    sink: Annotated[
        str,
        typer.Argument(help="Sink pin name."),
    ],
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, …)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
    descending: Annotated[
        bool,
        typer.Option("--descending/--ascending", help="Sort order."),
    ] = True,
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of paths to show."),
    ] = 0,
) -> None:
    """Rank all paths from source to sink by scalar delay."""
    _sdf, graph = _load_graph(sdf_file)

    ranked = rank_paths(graph, source, sink, field, metric, descending)
    if limit > 0:
        ranked = ranked[:limit]

    if not ranked:
        typer.echo("No paths found.")
        raise typer.Exit(code=1)

    for i, rp in enumerate(ranked):
        hops = " -> ".join([rp.edges[0].source] + [e.sink for e in rp.edges])
        typer.echo(f"#{i + 1}  scalar={rp.scalar}  {hops}")


@app.command()
def slack(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    source: Annotated[
        str,
        typer.Argument(help="Source pin name."),
    ],
    sink: Annotated[
        str,
        typer.Argument(help="Sink pin name."),
    ],
    period: Annotated[
        float,
        typer.Argument(help="Clock period or timing constraint."),
    ],
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, …)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
) -> None:
    """Compute slack for the critical path: period - critical_delay."""
    _sdf, graph = _load_graph(sdf_file)

    result = compute_slack(graph, source, sink, period, field, metric)
    if result is None:
        typer.echo("No path found or scalar is None.")
        raise typer.Exit(code=1)

    typer.echo(f"Slack: {result}")
    if result < 0:
        typer.echo("TIMING VIOLATION")


@app.command()
def dot(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file (default: stdout)."),
    ] = None,
    highlight_source: Annotated[
        str | None,
        typer.Option("--highlight-source", help="Source pin for highlight."),
    ] = None,
    highlight_sink: Annotated[
        str | None,
        typer.Option("--highlight-sink", help="Sink pin for critical path highlight."),
    ] = None,
    cluster: Annotated[
        bool,
        typer.Option("--cluster/--no-cluster", help="Cluster nodes by instance."),
    ] = False,
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, …)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
) -> None:
    """Export the timing graph as DOT (Graphviz) format."""
    _sdf, graph = _load_graph(sdf_file)

    highlight = None
    if highlight_source is not None and highlight_sink is not None:
        highlight = critical_path(
            graph, highlight_source, highlight_sink, field, metric
        )

    dot_str = to_dot(
        graph,
        highlight_path=highlight,
        cluster_by_instance=cluster,
        field=field,
        metric=metric,
    )

    if output is not None:
        output.write_text(dot_str)
        typer.echo(f"Written to {output}")
    else:
        typer.echo(dot_str)


@app.command()
def annotate(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF timing file."),
    ],
    verilog_file: Annotated[
        Path,
        typer.Argument(help="Path to the Verilog cell library file."),
    ],
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Output file (default: stdout)."),
    ] = None,
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, ...)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
) -> None:
    """Annotate a Verilog cell library with SDF timing specify blocks."""
    from sdf_toolkit.io.annotate import annotate_verilog

    result = annotate_verilog(
        sdf_path=sdf_file,
        verilog_path=verilog_file,
        output_path=output,
        field_name=field,
        metric=metric,
    )

    if output is None:
        typer.echo(result)
    else:
        typer.echo(f"Written to {output}")


@app.command()
def normalize(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    target: Annotated[
        str,
        typer.Option("--target", help="Target timescale (e.g. 1ns, 1ps)."),
    ],
    fmt: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format."),
    ] = OutputFormat.json,
) -> None:
    """Normalize all delays in an SDF file to a target timescale."""
    from sdf_toolkit.transform.normalize import normalize_delays

    sdf = _load_sdf(sdf_file)
    result = normalize_delays(sdf, target)

    if fmt == OutputFormat.json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        typer.echo(sdf_emit(result, timescale=target))


@app.command()
def lint(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    severity: Annotated[
        str,
        typer.Option("--severity", help="Filter by severity: error, warning, or all."),
    ] = "all",
) -> None:
    """Validate an SDF file and report structural/semantic issues."""
    from sdf_toolkit.analysis.validate import validate

    sdf = _load_sdf(sdf_file)
    issues = validate(sdf)

    if severity != "all":
        issues = [i for i in issues if i.severity == severity]

    if not issues:
        typer.echo("No issues found.")
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

    console.print(table)


@app.command()
def stats(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, ...)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
) -> None:
    """Compute aggregate statistics over delay values."""
    from sdf_toolkit.analysis.stats import compute_stats

    sdf = _load_sdf(sdf_file)
    result = compute_stats(sdf, field, metric)

    table = Table(title="SDF Statistics")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="green")

    table.add_row("Total cell types", str(result.total_cells))
    table.add_row("Total instances", str(result.total_instances))
    table.add_row("Total entries", str(result.total_entries))
    table.add_row("Delay min", str(result.delay_min))
    table.add_row("Delay max", str(result.delay_max))
    table.add_row("Delay mean", str(result.delay_mean))
    table.add_row("Delay median", str(result.delay_median))

    console.print(table)

    if result.entry_type_counts:
        type_table = Table(title="Entry Type Counts")
        type_table.add_column("Type", style="cyan")
        type_table.add_column("Count", style="green")
        for etype, count in sorted(result.entry_type_counts.items()):
            type_table.add_row(etype, str(count))
        console.print(type_table)


@app.command(name="query")
def query_cmd(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    cell_type: Annotated[
        list[str] | None,
        typer.Option("--cell-type", help="Filter by cell type (repeatable)."),
    ] = None,
    instance: Annotated[
        list[str] | None,
        typer.Option("--instance", help="Filter by instance name (repeatable)."),
    ] = None,
    entry_type: Annotated[
        list[str] | None,
        typer.Option("--entry-type", help="Filter by entry type (repeatable)."),
    ] = None,
    pin_pattern: Annotated[
        str | None,
        typer.Option("--pin-pattern", help="Regex to match from_pin or to_pin."),
    ] = None,
    min_delay: Annotated[
        float | None,
        typer.Option("--min-delay", help="Minimum delay threshold."),
    ] = None,
    max_delay: Annotated[
        float | None,
        typer.Option("--max-delay", help="Maximum delay threshold."),
    ] = None,
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, ...)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
    fmt: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format."),
    ] = OutputFormat.json,
) -> None:
    """Filter and query SDF file entries."""
    from sdf_toolkit.analysis.query import query

    sdf = _load_sdf(sdf_file)

    try:
        entry_types = [EntryType(e) for e in entry_type] if entry_type else None
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--entry-type") from exc

    result = query(
        sdf,
        cell_types=cell_type,
        instances=instance,
        entry_types=entry_types,
        pin_pattern=pin_pattern,
        min_delay=min_delay,
        max_delay=max_delay,
        field=field,
        metric=metric,
    )

    if fmt == OutputFormat.json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        typer.echo(sdf_emit(result, timescale=result.header.timescale or "1ps"))


@app.command(name="diff")
def diff_cmd(
    file_a: Annotated[
        Path,
        typer.Argument(help="Path to the first SDF file."),
    ],
    file_b: Annotated[
        Path,
        typer.Argument(help="Path to the second SDF file."),
    ],
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
) -> None:
    """Compare two SDF files and report differences."""
    from sdf_toolkit.analysis.diff import diff

    sdf_a = _load_sdf(file_a)
    sdf_b = _load_sdf(file_b)

    result = diff(
        sdf_a,
        sdf_b,
        tolerance=tolerance,
        normalize_first=normalize_first,
        target_timescale=target_timescale,
    )

    if result.header_diffs:
        header_table = Table(title="Header Differences")
        header_table.add_column("Field", style="cyan")
        header_table.add_column("File A", style="green")
        header_table.add_column("File B", style="yellow")
        for fld, (va, vb) in result.header_diffs.items():
            header_table.add_row(fld, str(va), str(vb))
        console.print(header_table)

    if result.only_in_a:
        typer.echo(f"\nOnly in A: {len(result.only_in_a)} entries")
        for ct, inst, en in result.only_in_a[:20]:
            typer.echo(f"  {ct}/{inst}/{en}")

    if result.only_in_b:
        typer.echo(f"\nOnly in B: {len(result.only_in_b)} entries")
        for ct, inst, en in result.only_in_b[:20]:
            typer.echo(f"  {ct}/{inst}/{en}")

    if result.value_diffs:
        diff_table = Table(title=f"Value Differences ({len(result.value_diffs)} total)")
        diff_table.add_column("Cell/Instance/Entry", style="cyan")
        diff_table.add_column("Field", style="cyan")
        diff_table.add_column("A", style="green")
        diff_table.add_column("B", style="yellow")
        diff_table.add_column("Delta", style="red")
        for d in result.value_diffs[:50]:
            diff_table.add_row(
                f"{d.cell_type}/{d.instance}/{d.entry_name}",
                d.field,
                str(d.value_a),
                str(d.value_b),
                str(d.delta),
            )
        console.print(diff_table)

    if (
        not result.header_diffs
        and not result.only_in_a
        and not result.only_in_b
        and not result.value_diffs
    ):
        typer.echo("Files are identical.")


@app.command(name="merge")
def merge_cmd(
    files: Annotated[
        list[Path],
        typer.Argument(help="SDF files to merge (at least 2)."),
    ],
    strategy: Annotated[
        str,
        typer.Option(
            "--strategy",
            help="Conflict strategy: keep-first, keep-last, error.",
        ),
    ] = "keep-last",
    target_timescale: Annotated[
        str | None,
        typer.Option(
            "--target-timescale",
            help="Normalize to this timescale before merging.",
        ),
    ] = None,
    fmt: Annotated[
        OutputFormat,
        typer.Option("--format", "-f", help="Output format."),
    ] = OutputFormat.json,
) -> None:
    """Merge two or more SDF files into one."""
    from sdf_toolkit.transform.merge import ConflictStrategy, merge

    try:
        conflict_strategy = ConflictStrategy(strategy)
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="--strategy") from exc

    sdf_files = [_load_sdf(f) for f in files]
    result = merge(
        sdf_files,
        strategy=conflict_strategy,
        target_timescale=target_timescale,
    )

    if fmt == OutputFormat.json:
        typer.echo(json.dumps(result.to_dict(), indent=2))
    else:
        typer.echo(sdf_emit(result, timescale=result.header.timescale or "1ps"))


@app.command(name="batch-analysis")
def batch_analysis_cmd(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, ...)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of results to show."),
    ] = 20,
) -> None:
    """Analyze all startpoint-to-endpoint pairs in the timing graph."""
    _sdf, graph = _load_graph(sdf_file)

    results = batch_endpoint_analysis(graph, field, metric)
    if limit > 0:
        results = results[:limit]

    if not results:
        typer.echo("No endpoint pairs found.")
        raise typer.Exit(code=1)

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

    console.print(table)


@app.command()
def report(
    sdf_file: Annotated[
        Path,
        typer.Argument(help="Path to the SDF file."),
    ],
    field: Annotated[
        str,
        typer.Option("--field", help="Delay field (nominal, fast, slow, ...)."),
    ] = "slow",
    metric: Annotated[
        str,
        typer.Option("--metric", help="Metric (min, avg, max)."),
    ] = "max",
    top_n: Annotated[
        int,
        typer.Option("--top-n", help="Number of top critical paths to show."),
    ] = 10,
    period: Annotated[
        float | None,
        typer.Option("--period", help="Clock period for slack analysis."),
    ] = None,
) -> None:
    """Generate a comprehensive timing report."""
    from sdf_toolkit.analysis.report import generate_report

    sdf = _load_sdf(sdf_file)
    text = generate_report(
        sdf,
        field=field,
        metric=metric,
        top_n_paths=top_n,
        period=period,
    )
    typer.echo(text)


def main() -> None:
    """Entry point for the SDF timing CLI."""
    app()
