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

from sdf_toolkit.parser.grammar import hidden_patterns

# The scan tells the four kinds of match apart by their first character, so
# it rests on no hidden terminal starting with a parenthesis.  Both of them
# start with a quote or a slash today, and
# ``test_hidden_patterns_never_start_with_a_parenthesis`` holds them to it.
_HIDDEN_HEADS = frozenset('"/')


class Blocks(NamedTuple):
    """Where the blocks of one construct start and where the construct ends."""

    starts: list[int]
    """Offset of the ``(`` opening each block, in file order."""

    end: int
    """Offset of the ``)`` closing the construct the blocks sit in."""


@lru_cache
def _scanner(keyword: str) -> re.Pattern[str]:
    """Match a hidden region, a block opening, or a bare parenthesis.

    The hidden alternatives come first, so a parenthesis inside a quoted
    string or a comment is consumed as part of that region rather than
    counted as structure.
    """
    hidden = "|".join(hidden_patterns())
    return re.compile(rf"{hidden}|\(\s*{re.escape(keyword)}\b|[()]")


def find_blocks(text: str, *, keyword: str) -> Blocks:
    """Locate the ``(KEYWORD`` blocks written directly inside the construct.

    Parameters
    ----------
    text : str
        SDF text starting at the ``(`` of the construct holding the blocks:
        the whole file for the cells of a DELAYFILE, the slice of one
        ``(ABSOLUTE ...)`` for its entries.
    keyword : str
        Keyword opening a block, ``CELL`` for the blocks of a DELAYFILE.

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
    >>> find_blocks('(DELAYFILE (CELL (CELLTYPE "A") (INSTANCE i)))', keyword="CELL")
    Blocks(starts=[11], end=45)
    >>> find_blocks('(DELAYFILE (CELL (CELLTYPE "(CELL x)")))', keyword="CELL")
    Blocks(starts=[11], end=39)
    """
    starts: list[int] = []
    level = 0
    for match in _scanner(keyword).finditer(text):
        token = match.group()
        head = token[0]
        if head in _HIDDEN_HEADS:
            continue
        if head == ")":
            level -= 1
            if level == 0:
                return Blocks(starts=starts, end=match.start())
        else:
            # Longer than one character, so the keyword followed the "(".
            if level == 1 and len(token) > 1:
                starts.append(match.start())
            level += 1
    raise ValueError(
        f"No ')' closes the construct holding the {keyword} blocks; "
        f"the text is truncated"
    )
