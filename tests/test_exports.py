"""Public names of the package and its subpackages."""

from types import ModuleType

import pytest

import sdf_toolkit
from sdf_toolkit import analysis, core, io, parser, transform
from sdf_toolkit.errors import SDFToolkitError

_PATHGRAPH_ERRORS = [
    "ConditionalPinDelayError",
    "IncrementWithoutBaseError",
    "NetDelayError",
    "TimingGraphError",
    "TimingLoopError",
    "UnplacedDeviceDelayError",
    "WildcardInstanceError",
]
_ERRORS = [
    *_PATHGRAPH_ERRORS,
    "AnnotationError",
    "DriverResolutionError",
    "InterconnectConflictError",
    "MergeConflictError",
    "MergeError",
    "MissingDelayError",
    "SDFModelError",
    "SDFParseError",
    "SDFToolkitError",
    "TimescaleMismatchError",
    "UnsupportedConstructError",
    "UnsupportedDiffError",
    "YosysError",
]
_EXPORTS = [
    *((sdf_toolkit, name) for name in [*_ERRORS, "ARC_ATTR"]),
    *((analysis, name) for name in [*_PATHGRAPH_ERRORS, "ARC_ATTR"]),
    (io, "AnnotationError"),
    (io, "YosysError"),
    (analysis, "TimescaleMismatchError"),
    (analysis, "UnsupportedDiffError"),
    (core, "SDFModelError"),
    (parser, "SDFParseError"),
    (parser, "UnsupportedConstructError"),
    (transform, "DriverResolutionError"),
    (transform, "InterconnectConflictError"),
    (transform, "MergeConflictError"),
    (transform, "MergeError"),
]


@pytest.mark.parametrize(
    ("module", "name"), _EXPORTS, ids=[f"{m.__name__}.{n}" for m, n in _EXPORTS]
)
def test_name_is_exported(module: ModuleType, name: str) -> None:
    assert name in module.__all__
    assert name in vars(module)


@pytest.mark.parametrize("name", _ERRORS)
def test_error_derives_from_base(name: str) -> None:
    error = vars(sdf_toolkit)[name]
    assert issubclass(error, SDFToolkitError)
