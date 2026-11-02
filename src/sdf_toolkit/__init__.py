"""sdf_toolkit -- parse and emit Standard Delay Format (SDF) timing files."""

from sdf_toolkit.analysis import (
    DiffEntry,
    DiffResult,
    EndpointResult,
    EntryLocation,
    LintIssue,
    RankedPath,
    SDFStats,
    TimingEdge,
    TimingGraph,
    VerificationResult,
    batch_endpoint_analysis,
    compute_slack,
    compute_stats,
    critical_path,
    decompose_delay,
    diff,
    generate_report,
    query,
    rank_paths,
    to_dot,
    validate,
    verify_path,
)
from sdf_toolkit.core import (
    CellBuilder,
    Corner,
    DelayValues,
    EdgeSelect,
    MissingDelayError,
    SDFBuilder,
    SDFFile,
    SDFHeader,
    Timescale,
    Values,
)
from sdf_toolkit.errors import SDFToolkitError
from sdf_toolkit.io import annotate_verilog, emit, emit_sdf, parse
from sdf_toolkit.parser import parse_sdf, parse_sdf_file
from sdf_toolkit.transform import ConflictStrategy, merge, normalize_delays

__all__ = [
    # core
    "CellBuilder",
    "Corner",
    "DelayValues",
    "EdgeSelect",
    "MissingDelayError",
    "SDFBuilder",
    "SDFFile",
    "SDFHeader",
    "Timescale",
    "Values",
    # errors
    "SDFToolkitError",
    # parser
    "parse_sdf",
    "parse_sdf_file",
    # io
    "annotate_verilog",
    "emit",
    "emit_sdf",
    "parse",
    # analysis
    "DiffEntry",
    "DiffResult",
    "EntryLocation",
    "EndpointResult",
    "LintIssue",
    "RankedPath",
    "SDFStats",
    "TimingEdge",
    "TimingGraph",
    "VerificationResult",
    "batch_endpoint_analysis",
    "compute_slack",
    "compute_stats",
    "critical_path",
    "decompose_delay",
    "diff",
    "generate_report",
    "query",
    "rank_paths",
    "to_dot",
    "validate",
    "verify_path",
    # transform
    "ConflictStrategy",
    "merge",
    "normalize_delays",
]
