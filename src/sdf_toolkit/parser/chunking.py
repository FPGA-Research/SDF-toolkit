"""Find the boundaries of a repeated block inside an SDF construct.

Several SDF constructs hold a flat sequence of like blocks: the ``(CELL ...)``
blocks of a DELAYFILE, the entries of a cell's timing list.  Such a sequence
can be cut between two blocks and the pieces parsed separately, so this module
reports where the cuts may go, for the keyword of the block being cut between.
{mod}`sdf_toolkit.parser.parser` turns the cuts into chunks and merges the
results.
"""

import re
from functools import lru_cache
from typing import NamedTuple

from sdf_toolkit.parser.grammar import LexicalPatterns


class Blocks(NamedTuple):
    """Where the blocks of one construct start and where the construct ends."""

    starts: list[int]
    """Offset of the ``(`` opening each block, in file order."""

    end: int
    """Offset of the ``)`` closing the construct the blocks sit in."""


@lru_cache
def _scanner(keyword: str, patterns: LexicalPatterns) -> re.Pattern[str]:
    """Match a hidden region, a block opening, or a bare parenthesis.

    The hidden alternatives come first, so a parenthesis inside a quoted
    string, an escaped identifier or a comment is consumed as part of that
    region rather than counted as structure.  A block opening may hold
    anything the lexer ignores between its ``(`` and the keyword.
    """
    hidden = "|".join(patterns.hidden)
    gap = "|".join(patterns.ignored)
    return re.compile(rf"{hidden}|\((?:{gap})*{re.escape(keyword)}\b|[()]")


def find_blocks(text: str, *, keyword: str, patterns: LexicalPatterns) -> Blocks:
    """Locate the ``(KEYWORD`` blocks written directly inside the construct.

    Parameters
    ----------
    text : str
        SDF text starting at the ``(`` of the construct holding the blocks:
        the whole file for the cells of a DELAYFILE, the slice of one
        ``(ABSOLUTE ...)`` for its entries.
    keyword : str
        Keyword opening a block, ``CELL`` for the blocks of a DELAYFILE.
    patterns : LexicalPatterns
        Hidden and ignored terminal patterns of the parser the chunks go to.

    Returns
    -------
    Blocks
        The block offsets and the offset of the enclosing construct's closing
        parenthesis.  A block nested deeper, or hidden inside a quoted string
        or a comment, is skipped.

    Raises
    ------
    ValueError
        If the enclosing construct is never closed, which means *text* is
        truncated: parse it whole to get the position of the syntax error.

    Examples
    --------
    >>> patterns = LexicalPatterns(hidden=('"[^"]*"',), ignored=(" +",))
    >>> find_blocks(
    ...     '(DELAYFILE (CELL (CELLTYPE "A") (INSTANCE i)))',
    ...     keyword="CELL",
    ...     patterns=patterns,
    ... )
    Blocks(starts=[11], end=45)
    >>> find_blocks(
    ...     '(DELAYFILE (CELL (CELLTYPE "(CELL x)")))',
    ...     keyword="CELL",
    ...     patterns=patterns,
    ... )
    Blocks(starts=[11], end=39)
    """
    starts: list[int] = []
    level = 0
    for match in _scanner(keyword, patterns).finditer(text):
        token = match.group()
        match token[0]:
            case ")":
                level -= 1
                if level == 0:
                    return Blocks(starts=starts, end=match.start())
            case "(":
                # Longer than one character, so the keyword followed the "(".
                if level == 1 and len(token) > 1:
                    starts.append(match.start())
                level += 1
            case _:
                # A hidden region, none of which can open with a parenthesis.
                pass
    raise ValueError(
        f"No ')' closes the construct holding the {keyword} blocks; "
        f"the text is truncated"
    )
