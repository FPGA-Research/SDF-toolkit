"""Hold the SDF grammar and the parts of it other modules have to agree with.

The grammar file is the one statement of SDF's lexical rules, so the block
scan in {mod}`sdf_toolkit.parser.chunking` takes the patterns that hide a
parenthesis from here instead of restating them and drifting.
"""

from functools import lru_cache
from pathlib import Path

from lark import Lark

#: Rules the parser is built to start at: a whole file, and the two fragments
#: a split leaves behind.
START_RULES = ("start", "head", "body")

#: Terminals whose text may hold a parenthesis that is not structure.  The
#: unquoted STRING terminal cannot contain one, so these two are the whole
#: list.
HIDING_TERMINALS = ("QSTRING", "COMMENT")


def load_grammar() -> str:
    """Read the SDF grammar shipped beside this module."""
    grammar_path = (Path(__file__).parent / "sdf.lark").resolve()
    try:
        return grammar_path.read_text()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Grammar file not found: {grammar_path}") from exc


@lru_cache
def hidden_patterns() -> tuple[str, ...]:
    """Return the regexp of each terminal named in :data:`HIDING_TERMINALS`.

    Returns
    -------
    tuple[str, ...]
        The patterns as Lark compiles them, in the order of
        :data:`HIDING_TERMINALS`.

    Raises
    ------
    ValueError
        If the grammar no longer defines one of those terminals, which would
        leave the block scan counting parentheses it cannot see.
    """
    terminals = {
        terminal.name: terminal.pattern.to_regexp()
        for terminal in Lark(
            load_grammar(), parser="lalr", start=list(START_RULES)
        ).terminals
    }
    missing = [name for name in HIDING_TERMINALS if name not in terminals]
    if missing:
        raise ValueError(
            f"sdf.lark defines no {', '.join(missing)} terminal, so the block "
            f"scan cannot tell which parentheses are structure; rename the "
            f"terminal in HIDING_TERMINALS to match the grammar"
        )
    return tuple(terminals[name] for name in HIDING_TERMINALS)
