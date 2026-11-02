"""Structural and semantic validation for SDF files.

An ERROR marks data no reading can make consistent, such as a triple whose
min exceeds its max.  A WARNING marks legal SDF that is often a mistake, such
as a missing TIMESCALE.  An empty triple ``()`` is legal SDF for an absent
value and is not reported.
"""

from dataclasses import dataclass
from enum import StrEnum
from itertools import pairwise

from sdf_toolkit.core.model import Corner, Entry, SDFFile, labelled_values

# ``(INSTANCE *)`` applies a cell's timing to every instance of its type.
_WILDCARD_INSTANCE = "*"


class IssueSeverity(StrEnum):
    """Severity levels for validation issues."""

    ERROR = "error"
    WARNING = "warning"


@dataclass
class LintIssue:
    """A single validation issue found in an SDF file.

    Attributes
    ----------
    severity : IssueSeverity
        Either ``IssueSeverity.ERROR`` or ``IssueSeverity.WARNING``.
    cell_type : str | None
        The cell type where the issue was found, or None when the issue
        is not tied to one.
    instance : str | None
        The instance name where the issue was found, or None when the
        issue is not tied to one.  ``""`` is the top-level instance.
    entry_name : str | None
        The arc key of the entry as SDF-like text, or None when the issue
        is not tied to one entry.
    message : str
        Human-readable description of the issue.
    """

    severity: IssueSeverity
    cell_type: str | None
    instance: str | None
    entry_name: str | None
    message: str


def _check_header(sdf: SDFFile) -> list[LintIssue]:
    """Check the SDF header for missing fields.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to validate.

    Returns
    -------
    list[LintIssue]
        Header-level issues found.
    """
    if sdf.header.timescale is None:
        return [
            LintIssue(
                severity=IssueSeverity.WARNING,
                cell_type=None,
                instance=None,
                entry_name=None,
                message="Missing timescale in header; the SDF default of 1ns applies",
            )
        ]
    return []


def _check_empty_cells(sdf: SDFFile) -> list[LintIssue]:
    """Check whether the SDF file has an empty cells dictionary.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to validate.

    Returns
    -------
    list[LintIssue]
        A single-element list if cells is empty, otherwise empty.
    """
    if not sdf.cells:
        return [
            LintIssue(
                severity=IssueSeverity.WARNING,
                cell_type=None,
                instance=None,
                entry_name=None,
                message="SDF file contains no cells",
            )
        ]
    return []


def _check_corner_order(
    entry: Entry,
    *,
    cell_type: str,
    instance: str,
) -> list[LintIssue]:
    """Report each value triple of *entry* that breaks min <= typ <= max.

    Returns
    -------
    list[LintIssue]
        One error per out-of-order triple.
    """
    issues: list[LintIssue] = []
    for label, values in labelled_values(entry):
        present = [(c, v) for c in Corner if (v := values.at(c)) is not None]
        for (low_corner, low), (high_corner, high) in pairwise(present):
            if low > high:
                issues.append(
                    LintIssue(
                        severity=IssueSeverity.ERROR,
                        cell_type=cell_type,
                        instance=instance,
                        entry_name=str(entry.key),
                        message=(
                            f"'{label}' breaks min <= typ <= max: "
                            f"{low_corner} {low} > {high_corner} {high}"
                        ),
                    )
                )
                break
    return issues


def _check_cross_cell_type_instances(sdf: SDFFile) -> list[LintIssue]:
    """Detect named instances that appear under multiple cell types.

    ``(INSTANCE *)`` names no instance, so it may sit under any number of
    cell types.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to validate.

    Returns
    -------
    list[LintIssue]
        Warnings for instances found under more than one cell type.
    """
    issues: list[LintIssue] = []
    instance_to_cell_types: dict[str, list[str]] = {}

    for cell_type, instances in sdf.cells.items():
        for instance in instances:
            if instance != _WILDCARD_INSTANCE:
                instance_to_cell_types.setdefault(instance, []).append(cell_type)

    for instance, cell_types in instance_to_cell_types.items():
        if len(cell_types) > 1:
            cell_types_str = ", ".join(sorted(cell_types))
            issues.append(
                LintIssue(
                    severity=IssueSeverity.WARNING,
                    cell_type=None,
                    instance=instance,
                    entry_name=None,
                    message=(
                        f"Instance '{instance}' appears under multiple "
                        f"cell types: {cell_types_str}"
                    ),
                )
            )
    return issues


def validate(sdf: SDFFile) -> list[LintIssue]:
    """Check an SDF file for structural and semantic issues.

    Parameters
    ----------
    sdf : SDFFile
        The SDF file to validate.

    Returns
    -------
    list[LintIssue]
        All issues found, ordered by severity (errors first).

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.analysis.validate import validate
    >>> from sdf_toolkit.core.model import DelayValues, PortSpec, Timescale, Values
    >>> buf = dict(
    ...     input=PortSpec("A"),
    ...     output=PortSpec("Y"),
    ...     delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ... )
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(timescale=Timescale.parse("1ps"))
    ...     .add_cell("BUF", "b0")
    ...     .add_iopath(**buf)
    ...     .build()
    ... )
    >>> issues = validate(sdf)
    >>> len(issues)
    0

    Missing timescale produces a warning:

    >>> sdf_no_ts = SDFBuilder().add_cell("BUF", "b0").add_iopath(**buf).build()
    >>> issues = validate(sdf_no_ts)
    >>> issues[0].message
    'Missing timescale in header; the SDF default of 1ns applies'
    """
    issues: list[LintIssue] = []

    # 1. Header checks
    issues.extend(_check_header(sdf))

    # 2. Empty cells check
    issues.extend(_check_empty_cells(sdf))

    # 3. Per-entry checks
    for cell_type, instances in sdf.cells.items():
        for instance, entries in instances.items():
            for entry in entries:
                issues.extend(
                    _check_corner_order(entry, cell_type=cell_type, instance=instance)
                )

    # 4. Cross-cell-type instance reuse
    issues.extend(_check_cross_cell_type_instances(sdf))

    # Sort: errors first, then warnings
    severity_order = {IssueSeverity.ERROR: 0, IssueSeverity.WARNING: 1}
    issues.sort(key=lambda issue: severity_order[issue.severity])

    return issues
