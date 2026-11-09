"""I/O modules for writing, annotating, and emitting SDF timing files.

Parsing has moved to :mod:`sdf_toolkit.parser`.  The ``parse_sdf`` and
``parse_sdf_file`` names are re-exported here for backward compatibility.
"""

from sdf_toolkit.io.annotate import annotate_verilog
from sdf_toolkit.io.sdfparse import emit, parse
from sdf_toolkit.io.transport import annotate_transport
from sdf_toolkit.io.writer import emit_sdf
from sdf_toolkit.io.yosys import AnnotationError, YosysError
from sdf_toolkit.parser import parse_sdf, parse_sdf_file

__all__ = [
    # yosys, annotate, transport
    "AnnotationError",
    "YosysError",
    "annotate_transport",
    "annotate_verilog",
    # parser (re-exported for backward compatibility)
    "parse_sdf",
    "parse_sdf_file",
    # sdfparse (legacy API)
    "emit",
    "parse",
    # writer
    "emit_sdf",
]
