"""Data models for SDF timing specifications."""

import operator
from collections.abc import Callable, ItemsView, KeysView, ValuesView
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any, Literal


class EntryType(StrEnum):
    """Types of SDF timing entries."""

    PORT = "port"
    INTERCONNECT = "interconnect"
    IOPATH = "iopath"
    DEVICE = "device"
    SETUP = "setup"
    HOLD = "hold"
    REMOVAL = "removal"
    RECOVERY = "recovery"
    WIDTH = "width"
    SETUPHOLD = "setuphold"
    PATHCONSTRAINT = "pathconstraint"


class EdgeType(StrEnum):
    """Port edge types in SDF timing specifications."""

    POSEDGE = "posedge"
    NEGEDGE = "negedge"


class DelayField(StrEnum):
    """Delay path field names in DelayPaths."""

    NOMINAL = "nominal"
    FAST = "fast"
    SLOW = "slow"
    SETUP = "setup"
    HOLD = "hold"
    RISE = "rise"
    FALL = "fall"


class DelayMetric(StrEnum):
    """Metric names within a Values triple."""

    MIN = "min"
    AVG = "avg"
    MAX = "max"


DelayFieldLike = DelayField | Literal["nominal", "fast", "slow", "setup", "hold", "rise", "fall"]
DelayMetricLike = DelayMetric | Literal["min", "avg", "max"]


class HeaderField(StrEnum):
    """SDF header field names."""

    SDFVERSION = "sdfversion"
    DESIGN = "design"
    VENDOR = "vendor"
    PROGRAM = "program"
    VERSION = "version"
    DIVIDER = "divider"
    DATE = "date"
    VOLTAGE = "voltage"
    PROCESS = "process"
    TEMPERATURE = "temperature"
    TIMESCALE = "timescale"


@dataclass(frozen=True, slots=True)
class PortSpec:
    """Port specification with name and optional edge."""

    port: str
    port_edge: EdgeType | None = None


@dataclass(frozen=True, slots=True)
class TimingPortSpec(PortSpec):
    """Port specification for timing checks, with condition info."""

    cond: bool = False
    cond_equation: str | None = None


@dataclass
class SDFHeader:
    """SDF file header containing metadata fields."""

    sdfversion: str | None = None
    design: str | None = None
    vendor: str | None = None
    program: str | None = None
    version: str | None = None
    divider: str | None = None
    date: str | None = None
    voltage: str | None = None
    process: str | None = None
    temperature: str | None = None
    timescale: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Return non-None header fields as a dictionary."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    def __getitem__(self, key: str) -> str | None:
        """Return header field by name."""
        return getattr(self, key)

    def __contains__(self, key: str) -> bool:
        """Check whether header field is set."""
        return getattr(self, key, None) is not None

    def get(self, key: str, default: str | None = None) -> str | None:
        """Return the header field *key*, falling back to *default* via getattr."""
        return getattr(self, key, default)

    def keys(self) -> KeysView[str]:
        """Return keys of non-None header fields."""
        return self.to_dict().keys()

    def values(self) -> ValuesView[str]:
        """Return values of non-None header fields."""
        return self.to_dict().values()

    def items(self) -> ItemsView[str, str]:
        """Return key-value pairs of non-None header fields."""
        return self.to_dict().items()


@dataclass
class Values:
    """Min/avg/max timing value triple."""

    min: float | None = None
    avg: float | None = None
    max: float | None = None

    def to_dict(self) -> dict[str, float | None]:
        """Return the triple as a dictionary."""
        return {"min": self.min, "avg": self.avg, "max": self.max}

    def _binop(
        self,
        other: "Values",
        op: Callable[[float, float], float],
    ) -> "Values":
        """Apply a binary operation element-wise.

        Parameters
        ----------
        other : Values
            The other Values triple.
        op : Callable[[float, float], float]
            The binary operation to apply.

        Returns
        -------
        Values
            A new Values with the operation applied. None propagates
            None.
        """

        def _apply(
            a: float | None,
            b: float | None,
        ) -> float | None:
            if a is not None and b is not None:
                return op(a, b)
            return None

        return Values(
            min=_apply(self.min, other.min),
            avg=_apply(self.avg, other.avg),
            max=_apply(self.max, other.max),
        )

    def __add__(self, other: "Values") -> "Values":
        """Element-wise addition of two Values triples.

        Parameters
        ----------
        other : Values
            The other Values triple to add.

        Returns
        -------
        Values
            A new Values with element-wise sums. None propagates None.
        """
        return self._binop(other, operator.add)

    def __sub__(self, other: "Values") -> "Values":
        """Element-wise subtraction of two Values triples.

        Parameters
        ----------
        other : Values
            The other Values triple to subtract.

        Returns
        -------
        Values
            A new Values with element-wise differences. None propagates
            None.
        """
        return self._binop(other, operator.sub)

    def _map_fields(
        self,
        fn: Callable[[float], float],
    ) -> "Values":
        """Apply a unary function to each non-None field.

        Parameters
        ----------
        fn : Callable[[float], float]
            The function to apply to each field value.

        Returns
        -------
        Values
            A new Values with the function applied. None stays None.
        """
        return Values(
            min=fn(self.min) if self.min is not None else None,
            avg=fn(self.avg) if self.avg is not None else None,
            max=fn(self.max) if self.max is not None else None,
        )

    def __neg__(self) -> "Values":
        """Negate all fields.

        Returns
        -------
        Values
            A new Values with all fields negated. None stays None.
        """
        return self._map_fields(operator.neg)

    def __mul__(self, scalar: float) -> "Values":
        """Scalar multiplication.

        Parameters
        ----------
        scalar : float
            The scalar to multiply by.

        Returns
        -------
        Values
            A new Values with all fields multiplied. None stays None.
        """
        return self._map_fields(lambda v: v * scalar)

    def __rmul__(self, scalar: float) -> "Values":
        """Reverse scalar multiplication, enables ``scalar * Values(...)``."""
        return self.__mul__(scalar)

    def approx_eq(self, other: "Values", tolerance: float = 1e-9) -> bool:
        """Floating-point tolerant comparison.

        Parameters
        ----------
        other : Values
            The other Values triple to compare against.
        tolerance : float, optional
            The absolute tolerance for comparison, by default 1e-9.

        Returns
        -------
        bool
            True if all fields are equal within tolerance, or both None.
        """
        pairs = [
            (self.min, other.min),
            (self.avg, other.avg),
            (self.max, other.max),
        ]
        for s, o in pairs:
            if s is None and o is None:
                continue
            if s is None or o is None:
                return False
            if abs(s - o) > tolerance:
                return False
        return True

    def __hash__(self) -> int:
        """Return hash based on the triple of (min, avg, max).

        Returns
        -------
        int
            Hash value.
        """
        return hash((self.min, self.avg, self.max))


@dataclass
class DelayPaths:
    """Collection of delay paths (nominal, fast, slow, etc.)."""

    nominal: Values | None = None
    fast: Values | None = None
    slow: Values | None = None
    # For constraints like setuphold
    setup: Values | None = None
    hold: Values | None = None
    # For path constraints
    rise: Values | None = None
    fall: Values | None = None

    def get_scalar(self, field: DelayFieldLike = DelayField.SLOW, metric: DelayMetricLike = DelayMetric.MAX) -> float | None:
        """Extract a single float from a named field and metric.

        Parameters
        ----------
        field : str
            One of the ``DelayField`` values (nominal, fast, slow, …).
        metric : str
            One of the ``DelayMetric`` values (min, avg, max).

        Returns
        -------
        float | None
            The scalar value, or None if the field or metric is None.

        Raises
        ------
        ValueError
            If *field* or *metric* is not a valid name.
        """
        # Check if field is valid (Python 3.11 compatible)
        if field not in DelayField.__members__.values():
            msg = f"Invalid field {field!r}, expected one of {tuple(DelayField)}"
            raise ValueError(msg)
        # Check if metric is valid (Python 3.11 compatible)
        if metric not in DelayMetric.__members__.values():
            msg = f"Invalid metric {metric!r}, expected one of {tuple(DelayMetric)}"
            raise ValueError(msg)
        values: Values | None = getattr(self, field)
        if values is None:
            return None
        return getattr(values, metric)

    def to_dict(self) -> dict[str, dict[str, float | None]]:
        """Return non-None delay paths as a dictionary."""
        return {
            name: val.to_dict()
            for name in DelayField
            if (val := getattr(self, name)) is not None
        }

    def __contains__(self, key: str) -> bool:
        """Check whether a delay path is set."""
        return getattr(self, key, None) is not None

    def __getitem__(self, key: str) -> Values | None:
        """Return delay path by name."""
        return getattr(self, key)

    def _binop(
        self,
        other: "DelayPaths",
        op: Callable[[Values, Values], Values],
    ) -> "DelayPaths":
        """Apply a binary operation field-wise across two DelayPaths.

        Parameters
        ----------
        other : DelayPaths
            The other DelayPaths.
        op : Callable[[Values, Values], Values]
            The binary operation to apply per field.

        Returns
        -------
        DelayPaths
            A new DelayPaths with the operation applied. None propagates None.
        """
        kwargs: dict[str, Values | None] = {}
        for name in DelayField:
            a = getattr(self, name)
            b = getattr(other, name)
            kwargs[name] = op(a, b) if a is not None and b is not None else None
        return DelayPaths(**kwargs)

    def __add__(self, other: "DelayPaths") -> "DelayPaths":
        """Field-wise addition of two DelayPaths.

        Parameters
        ----------
        other : DelayPaths
            The other DelayPaths to add.

        Returns
        -------
        DelayPaths
            A new DelayPaths with field-wise sums. None propagates None.
        """
        return self._binop(other, operator.add)

    def __sub__(self, other: "DelayPaths") -> "DelayPaths":
        """Field-wise subtraction of two DelayPaths.

        Parameters
        ----------
        other : DelayPaths
            The other DelayPaths to subtract.

        Returns
        -------
        DelayPaths
            A new DelayPaths with field-wise differences. None propagates None.
        """
        return self._binop(other, operator.sub)

    def approx_eq(self, other: "DelayPaths", tolerance: float = 1e-9) -> bool:
        """Floating-point tolerant comparison of two DelayPaths.

        Parameters
        ----------
        other : DelayPaths
            The other DelayPaths to compare against.
        tolerance : float, optional
            The absolute tolerance for comparison, by default 1e-9.

        Returns
        -------
        bool
            True if all fields are approximately equal or both None.
        """
        for name in DelayField:
            a = getattr(self, name)
            b = getattr(other, name)
            if a is None and b is None:
                continue
            if a is None or b is None:
                return False
            if not a.approx_eq(b, tolerance):
                return False
        return True


@dataclass
class BaseEntry:
    """Base class for all SDF timing entries."""

    name: str = ""
    type: EntryType = EntryType.IOPATH
    from_pin: str | None = None
    to_pin: str | None = None
    from_pin_edge: EdgeType | None = None
    to_pin_edge: EdgeType | None = None
    delay_paths: DelayPaths | None = None
    cond_equation: str | None = None
    is_timing_check: bool = False
    is_timing_env: bool = False
    is_absolute: bool = False
    is_incremental: bool = False
    is_cond: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Return all entry fields as a dictionary."""
        return asdict(self)


# Delays
@dataclass
class Port(BaseEntry):
    """Port delay entry."""

    def __post_init__(self) -> None:
        """Set entry type to PORT."""
        self.type = EntryType.PORT


@dataclass
class Interconnect(BaseEntry):
    """Interconnect delay entry."""

    def __post_init__(self) -> None:
        """Set entry type to INTERCONNECT."""
        self.type = EntryType.INTERCONNECT


@dataclass
class Iopath(BaseEntry):
    """IOPATH delay entry."""

    def __post_init__(self) -> None:
        """Set entry type to IOPATH."""
        self.type = EntryType.IOPATH


@dataclass
class Device(BaseEntry):
    """Device delay entry."""

    def __post_init__(self) -> None:
        """Set entry type to DEVICE."""
        self.type = EntryType.DEVICE


# Timing Checks
@dataclass
class TimingCheck(BaseEntry):
    """Base class for timing check entries."""

    is_timing_check: bool = True


@dataclass
class Setup(TimingCheck):
    """Setup timing check entry."""

    def __post_init__(self) -> None:
        """Set entry type to SETUP."""
        self.type = EntryType.SETUP


@dataclass
class Hold(TimingCheck):
    """Hold timing check entry."""

    def __post_init__(self) -> None:
        """Set entry type to HOLD."""
        self.type = EntryType.HOLD


@dataclass
class Removal(TimingCheck):
    """Removal timing check entry."""

    def __post_init__(self) -> None:
        """Set entry type to REMOVAL."""
        self.type = EntryType.REMOVAL


@dataclass
class Recovery(TimingCheck):
    """Recovery timing check entry."""

    def __post_init__(self) -> None:
        """Set entry type to RECOVERY."""
        self.type = EntryType.RECOVERY


@dataclass
class Width(TimingCheck):
    """Width timing check entry."""

    def __post_init__(self) -> None:
        """Set entry type to WIDTH."""
        self.type = EntryType.WIDTH


@dataclass
class SetupHold(TimingCheck):
    """SetupHold combined timing check entry."""

    def __post_init__(self) -> None:
        """Set entry type to SETUPHOLD."""
        self.type = EntryType.SETUPHOLD


# Constraints
@dataclass
class PathConstraint(BaseEntry):
    """Path constraint timing environment entry."""

    is_timing_env: bool = True

    def __post_init__(self) -> None:
        """Set entry type to PATHCONSTRAINT."""
        self.type = EntryType.PATHCONSTRAINT


# Type alias for the cells dictionary structure
CellsDict = dict[str, dict[str, dict[str, BaseEntry]]]

# Type for SDFFile.get() and __getitem__ return values
SDFFileValue = SDFHeader | CellsDict


@dataclass
class SDFFile:
    """Top-level SDF file containing header and cell timing data."""

    header: SDFHeader = field(default_factory=SDFHeader)
    cells: CellsDict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the full SDF structure as a nested dictionary."""
        cells_dict = {
            cell_type: {
                inst: {name: entry.to_dict() for name, entry in entries.items()}
                for inst, entries in instances.items()
            }
            for cell_type, instances in self.cells.items()
        }
        return {"header": self.header.to_dict(), "cells": cells_dict}

    def __getitem__(self, key: str) -> SDFFileValue:
        """Return header or cells by key."""
        if key == "header":
            return self.header
        if key == "cells":
            return self.cells
        raise KeyError(key)

    def __contains__(self, key: str) -> bool:
        """Check whether key is 'header' or 'cells'."""
        return key in ("header", "cells")

    def get(self, key: str, default: SDFFileValue | None = None) -> SDFFileValue | None:
        """Return header or cells by key, with optional default."""
        try:
            return self[key]
        except KeyError:
            return default
