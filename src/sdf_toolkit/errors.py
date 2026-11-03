"""Common base of every error the library raises for bad input data.

Each library error subclasses {class}`SDFToolkitError` and keeps its stdlib
base, such as ``ValueError``, so ``except SDFToolkitError`` catches every
data error the library raises and existing ``except ValueError`` callers keep
working.  The CLI catches {class}`SDFToolkitError` in one place and reports it
as ``Error: <message>`` with exit code 1.  This module imports nothing from
the package, so every other module can import it without a cycle.
"""


class SDFToolkitError(Exception):
    """Base class of every error sdf-toolkit raises for bad input data."""
