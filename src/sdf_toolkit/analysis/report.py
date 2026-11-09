"""Generate human-readable timing reports from SDF files."""

from decimal import Decimal
from io import StringIO

from rich.console import Console
from rich.table import Table

from sdf_toolkit.analysis.paths import batch_endpoint_analysis, compute_slack
from sdf_toolkit.analysis.stats import compute_stats
from sdf_toolkit.analysis.validate import validate
from sdf_toolkit.core.enums import Corner, EdgeSelect
from sdf_toolkit.core.pathgraph import TimingGraph
from sdf_toolkit.core.sdffile import SDFFile
from sdf_toolkit.core.values import Number, to_decimal


def _format_delay(value: Decimal | None) -> str:
    """Format a delay for display, or return 'N/A' if None.

    Parameters
    ----------
    value : Decimal | None
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
    *,
    corner: Corner = Corner.MAX,
    edge: EdgeSelect = EdgeSelect.WORST,
    top_n_paths: int = 10,
    period: Number | None = None,
) -> str:
    """Generate a human-readable timing report.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to analyze.
    corner : Corner
        Process corner to read.
    edge : EdgeSelect
        How the rvalues of each delay reduce to one number.
    top_n_paths : int
        Number of top critical endpoint pairs to show.
    period : Number | None
        Optional clock period for slack analysis.

    Returns
    -------
    str
        A formatted text report.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.report import generate_report
    >>> from sdf_toolkit.core.values import DelayValues, Timescale, Values
    >>> from sdf_toolkit.core.enums import Divider
    >>> from sdf_toolkit.core.delays import PortSpec
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale=Timescale.parse("1ps"), divider=Divider.SLASH)
    ...     .add_cell("BUF", "b0")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .add_interconnect(
    ...         source=PortSpec("a/Y"),
    ...         load=PortSpec("b/A"),
    ...         delay=DelayValues.of(Values(min=0.5, typ=1.0, max=1.5)),
    ...     )
    ...     .build()
    ... )
    >>> report = generate_report(sdf)
    >>> isinstance(report, str)
    True
    >>> "Statistics" in report
    True
    """
    buf = StringIO()
    period = None if period is None else to_decimal(period)
    console = Console(file=buf, force_terminal=False, width=120)

    # Section 1: Header summary
    header_table = Table(title="SDF Header")
    header_table.add_column("Field")
    header_table.add_column("Value")
    for key, value in sdf.header.to_dict().items():
        header_table.add_row(str(key), value)
    console.print(header_table)

    # Section 2: Statistics
    stats = compute_stats(sdf, corner=corner, edge=edge)
    stats_table = Table(title="Statistics")
    stats_table.add_column("Metric")
    stats_table.add_column("Value")
    stats_table.add_row("Total Cells", str(stats.total_cells))
    stats_table.add_row("Total Instances", str(stats.total_instances))
    stats_table.add_row("Total Entries", str(stats.total_entries))
    stats_table.add_row("Delays Without Value", str(stats.missing_delay_count))
    stats_table.add_row("Delay Min", _format_delay(stats.delay_min))
    stats_table.add_row("Delay Max", _format_delay(stats.delay_max))
    stats_table.add_row("Delay Mean", _format_delay(stats.delay_mean))
    stats_table.add_row("Delay Median", _format_delay(stats.delay_median))
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
    endpoint_results = batch_endpoint_analysis(graph, corner=corner, edge=edge)
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
            _format_delay(result.critical_delay),
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
                graph,
                result.source,
                result.sink,
                period=period,
                corner=corner,
                edge=edge,
            )
            if slack is not None:
                status = "VIOLATION" if slack < 0 else "OK"
            else:
                status = "N/A"
            slack_table.add_row(
                result.source,
                result.sink,
                _format_delay(result.critical_delay),
                _format_delay(period),
                _format_delay(slack),
                status,
            )
        console.print(slack_table)

    return buf.getvalue()
