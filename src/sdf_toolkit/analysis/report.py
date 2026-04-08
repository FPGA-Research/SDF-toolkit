"""Generate human-readable timing reports from SDF files."""

from io import StringIO

from rich.console import Console
from rich.table import Table

from sdf_toolkit.analysis.stats import compute_stats
from sdf_toolkit.analysis.validate import validate
from sdf_toolkit.core.model import DelayField, DelayFieldLike, DelayMetric, DelayMetricLike, SDFFile
from sdf_toolkit.core.pathgraph import (
    TimingGraph,
    batch_endpoint_analysis,
    compute_slack,
)


def _format_float(value: float | None) -> str:
    """Format a float value for display, or return 'N/A' if None.

    Parameters
    ----------
    value : float | None
        The value to format.

    Returns
    -------
    str
        The formatted string representation.
    """
    if value is None:
        return "N/A"
    return f"{value:.6f}"


def generate_report(
    sdf: SDFFile,
    field: DelayFieldLike = DelayField.SLOW,
    metric: DelayMetricLike = DelayMetric.MAX,
    top_n_paths: int = 10,
    period: float | None = None,
) -> str:
    """Generate a human-readable timing report.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to analyze.
    field : str
        Delay field to use for analysis.
    metric : str
        Metric to use for analysis.
    top_n_paths : int
        Number of top critical endpoint pairs to show.
    period : float | None
        Optional clock period for slack analysis.

    Returns
    -------
    str
        A formatted text report.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.report import generate_report
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale="1ps")
    ...     .add_cell("BUF", "b0")
    ...         .add_iopath("A", "Y", {
    ...             "slow": {"min": 1.0, "avg": 2.0, "max": 3.0},
    ...         })
    ...         .add_interconnect("a/Y", "b/A", {
    ...             "slow": {"min": 0.5, "avg": 1.0, "max": 1.5},
    ...         })
    ...     .build()
    ... )
    >>> report = generate_report(sdf)
    >>> isinstance(report, str)
    True
    >>> "Statistics" in report
    True
    """
    buf = StringIO()
    console = Console(file=buf, force_terminal=False, width=120)

    # Section 1: Header summary
    header_table = Table(title="SDF Header")
    header_table.add_column("Field")
    header_table.add_column("Value")
    for key, value in sdf.header.items():
        header_table.add_row(key, str(value))
    console.print(header_table)

    # Section 2: Statistics
    stats = compute_stats(sdf, field, metric)
    stats_table = Table(title="Statistics")
    stats_table.add_column("Metric")
    stats_table.add_column("Value")
    stats_table.add_row("Total Cells", str(stats.total_cells))
    stats_table.add_row("Total Instances", str(stats.total_instances))
    stats_table.add_row("Total Entries", str(stats.total_entries))
    stats_table.add_row("Delay Min", _format_float(stats.delay_min))
    stats_table.add_row("Delay Max", _format_float(stats.delay_max))
    stats_table.add_row("Delay Mean", _format_float(stats.delay_mean))
    stats_table.add_row("Delay Median", _format_float(stats.delay_median))
    console.print(stats_table)

    # Section 3: Validation issues
    issues = validate(sdf)
    if issues:
        issues_table = Table(title="Validation Issues")
        issues_table.add_column("Severity")
        issues_table.add_column("Cell Type")
        issues_table.add_column("Instance")
        issues_table.add_column("Entry")
        issues_table.add_column("Message")
        for issue in issues:
            issues_table.add_row(
                issue.severity,
                issue.cell_type,
                issue.instance,
                issue.entry_name,
                issue.message,
            )
        console.print(issues_table)

    # Section 4: Top N critical endpoint pairs
    graph = TimingGraph(sdf)
    endpoint_results = batch_endpoint_analysis(graph, field, metric)
    top_results = endpoint_results[:top_n_paths]

    endpoints_table = Table(title="Top Critical Endpoint Pairs")
    endpoints_table.add_column("Rank")
    endpoints_table.add_column("Source")
    endpoints_table.add_column("Sink")
    endpoints_table.add_column("Critical Delay")
    endpoints_table.add_column("Path Count")
    for rank, result in enumerate(top_results, start=1):
        endpoints_table.add_row(
            str(rank),
            result.source,
            result.sink,
            _format_float(result.critical_delay),
            str(result.path_count),
        )
    console.print(endpoints_table)

    # Section 5: Slack analysis (only if period is given)
    if period is not None:
        slack_table = Table(title="Slack Analysis")
        slack_table.add_column("Source")
        slack_table.add_column("Sink")
        slack_table.add_column("Critical Delay")
        slack_table.add_column("Period")
        slack_table.add_column("Slack")
        slack_table.add_column("Status")
        for result in top_results:
            slack = compute_slack(
                graph, result.source, result.sink, period, field, metric
            )
            if slack is not None:
                status = "VIOLATION" if slack < 0 else "OK"
            else:
                status = "N/A"
            slack_table.add_row(
                result.source,
                result.sink,
                _format_float(result.critical_delay),
                _format_float(period),
                _format_float(slack),
                status,
            )
        console.print(slack_table)

    return buf.getvalue()
