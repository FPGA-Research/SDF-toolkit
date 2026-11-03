"""Hold the SDF grammar and the parts of it other modules have to agree with.

The grammar file is the one statement of SDF's lexical rules, so the block
scan in {mod}`sdf_toolkit.parser.chunking` reads the patterns that hide a
parenthesis from the built parser instead of restating them and drifting.
"""

from pathlib import Path
from typing import NamedTuple

from lark import Lark

#: Rules the parser is built to start at: a whole file, and the three
#: fragments a split leaves behind.
START_RULES = ("sdf_file", "head", "body", "tail")

#: Terminals whose text may hold a parenthesis that is not structure: a quoted
#: string, an identifier with an escaped ``\(``, and the two comment forms.
HIDING_TERMINALS = ("QSTRING", "STRING", "COMMENT", "BLOCK_COMMENT")


class LexicalPatterns(NamedTuple):
    """Regexps the block scan needs to see the parentheses the lexer sees."""

    hidden: tuple[str, ...]
    """Patterns of :data:`HIDING_TERMINALS`, in the order the lexer tries them."""

    ignored: tuple[str, ...]
    """Patterns of the ``%ignore`` terminals, which may sit between tokens."""


def load_grammar() -> str:
    """Read the SDF grammar shipped beside this module."""
    grammar_path = (Path(__file__).parent / "sdf.lark").resolve()
    try:
        return grammar_path.read_text()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Grammar file not found: {grammar_path}") from exc


def lexical_patterns(parser: Lark) -> LexicalPatterns:
    """Return the scan patterns as *parser* compiled them.

    Raises
    ------
    KeyError
        If the grammar no longer defines a terminal named in
        :data:`HIDING_TERMINALS`, which would leave the block scan counting
        parentheses it cannot see.
    """
    hiding = [parser.get_terminal(name) for name in HIDING_TERMINALS]
    # The key of lark.lexer.BasicLexer, so a slash the lexer reads as the
    # start of a comment is not read as a STRING by the scan.
    hiding.sort(
        key=lambda t: (-t.priority, -t.pattern.max_width, -len(t.pattern.value), t.name)
    )
    return LexicalPatterns(
        hidden=tuple(t.pattern.to_regexp() for t in hiding),
        ignored=tuple(
            parser.get_terminal(name).pattern.to_regexp()
            for name in parser.ignore_tokens
        ),
    )
