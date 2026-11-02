"""Analysis modules for SDF timing data."""

from sdf_toolkit.analysis.diff import (
    DiffEntry,
    DiffResult,
    EntryLocation,
    RvalueCountField,
    ValueField,
    ValueSlot,
    diff,
)
from sdf_toolkit.analysis.export import to_dot
from sdf_toolkit.analysis.query import query
from sdf_toolkit.analysis.report import generate_report
from sdf_toolkit.analysis.stats import SDFStats, compute_stats, count_entry_types
from sdf_toolkit.analysis.validate import IssueSeverity, LintIssue, validate
from sdf_toolkit.core.pathgraph import (
    EndpointResult,
    RankedPath,
    TimingEdge,
    TimingGraph,
    VerificationResult,
    batch_endpoint_analysis,
    compute_slack,
    critical_path,
    decompose_delay,
    rank_paths,
    verify_path,
)

__all__ = [
    # diff
    "DiffEntry",
    "DiffResult",
    "EntryLocation",
    "RvalueCountField",
    "ValueField",
    "ValueSlot",
    "diff",
    # export
    "to_dot",
    # pathgraph
    "EndpointResult",
    "RankedPath",
    "TimingEdge",
    "TimingGraph",
    "VerificationResult",
    "batch_endpoint_analysis",
    "compute_slack",
    "critical_path",
    "decompose_delay",
    "rank_paths",
    "verify_path",
    # query
    "query",
    # report
    "generate_report",
    # stats
    "SDFStats",
    "compute_stats",
    "count_entry_types",
    # validate
    "IssueSeverity",
    "LintIssue",
    "validate",
]
