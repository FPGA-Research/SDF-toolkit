r"""SDF identifier escaping.

IEEE 1497 lets an identifier hold letters, digits and ``_``; any other
character is written with a preceding backslash, so the Verilog instance
``u_inv[0]`` appears in SDF as ``u_inv\[0\]``.  The parser keeps names in
that escaped form, so the file round-trips unchanged.  Netlist tools such as
Yosys report the bare name, and a user types the bare name, so every
comparison against them goes through {func}`sdf_unescape`.
"""

import string

from sdf_toolkit.core.enums import Divider
from sdf_toolkit.errors import SDFToolkitError


class SDFNameError(SDFToolkitError, ValueError):
    """An SDF name breaks the IEEE 1497 escape rules."""


_PLAIN = frozenset(string.ascii_letters + string.digits + "_")


def sdf_unescape(name: str) -> str:
    r"""Return *name* with each escape ``\c`` replaced by the character ``c``.

    Unescaped characters, including hierarchy dividers and bit-select
    brackets, pass through, so a whole SDF path unescapes in one call.

    Raises
    ------
    SDFNameError
        If *name* ends in a backslash that escapes nothing.

    Examples
    --------
    >>> sdf_unescape(r"u_inv\[0\]/A")
    'u_inv[0]/A'
    >>> sdf_unescape("x\\")  # doctest: +IGNORE_EXCEPTION_DETAIL
    Traceback (most recent call last):
    ...
    SDFNameError: SDF name 'x\\' ends in a backslash that escapes nothing
    """
    chars: list[str] = []
    escaped = False
    for char in name:
        if escaped:
            chars.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            chars.append(char)
    if escaped:
        msg = f"SDF name {name!r} ends in a backslash that escapes nothing"
        raise SDFNameError(msg)
    return "".join(chars)


def sdf_escape(name: str) -> str:
    r"""Write the bare identifier *name* in SDF form, escaping every special character.

    *name* is one identifier, so a divider inside it is escaped too.

    Examples
    --------
    >>> print(sdf_escape("g[0].u_inv"))
    g\[0\]\.u_inv
    """
    return "".join(c if c in _PLAIN else f"\\{c}" for c in name)


def split_path(path: str, divider: Divider) -> tuple[str, ...]:
    r"""Split an SDF path at every *divider* that is not escaped.

    The parts keep their escapes.

    Examples
    --------
    >>> split_path(r"top/u\/x/A", Divider.SLASH)
    ('top', 'u\\/x', 'A')
    """
    parts: list[str] = []
    current: list[str] = []
    escaped = False
    for char in path:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\":
            current.append(char)
            escaped = True
        elif char == divider:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return tuple(parts)
