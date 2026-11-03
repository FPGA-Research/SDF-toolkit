"""Error raised when SDF text does not match the grammar.

The serial and the chunked parse both raise {class}`SDFParseError`, so the
size of a file never decides which class a caller catches.  It subclasses
``ValueError`` because the model raises ``ValueError`` for text that matches
the grammar but breaks an IEEE 1497 rule, and one ``except`` covers both.
"""

from functools import partial

from lark import UnexpectedCharacters, UnexpectedEOF, UnexpectedInput, UnexpectedToken

from sdf_toolkit.errors import SDFToolkitError


class SDFParseError(SDFToolkitError, ValueError):
    """SDF text does not match the grammar at *line* and *column* (1-based)."""

    def __init__(
        self, message: str, *, line: int, column: int, expected: frozenset[str]
    ) -> None:
        super().__init__(message)
        self.line = line
        self.column = column
        self.expected = expected

    def __reduce__(self) -> tuple[partial["SDFParseError"], tuple[str]]:
        """Pickle the keyword fields too, so a worker process can raise it."""
        rebuild = partial(
            type(self), line=self.line, column=self.column, expected=self.expected
        )
        return rebuild, (str(self),)

    @classmethod
    def from_lark(cls, error: UnexpectedInput, text: str) -> "SDFParseError":
        """Describe *error*, raised while parsing *text*, with its context line."""
        match error:
            case UnexpectedCharacters():
                found = repr(error.char)
                expected = frozenset(error.allowed)
            case UnexpectedToken(token=token) if token.type != "$END":
                found = f"{token.type} {str(token)!r}"
                expected = frozenset(error.expected)
            case UnexpectedToken() | UnexpectedEOF():
                found = "end of input"
                expected = frozenset(str(name) for name in error.expected)
            case _:
                msg = f"Unhandled Lark error type {type(error).__name__}"
                raise TypeError(msg) from error
        message = (
            f"SDF parsing failed at {error.line}:{error.column} - found {found}, "
            f"expected one of {sorted(expected)}\n{error.get_context(text)}"
        )
        return cls(message, line=error.line, column=error.column, expected=expected)


class UnsupportedConstructError(SDFParseError):
    """SDF text uses a construct the grammar names only to reject it."""


class StandardViolationError(SDFParseError):
    """SDF text uses a construct the rules of the parse forbid.

    The message names the construct, the standard or option that forbids it
    and the switch that reads it.
    """


class StandardMismatchError(SDFToolkitError, ValueError):
    """The SDFVERSION of a file names a standard other than the one asked for."""
