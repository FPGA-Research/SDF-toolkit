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

# Only a quoted string or a comment can hide a parenthesis from the depth
# count: the grammar's unquoted STRING terminal cannot contain one.  Both
# patterns restate terminals of sdf.lark, which
# ``test_mask_hides_what_the_grammar_hides`` holds them to.
_HIDDEN = re.compile(r'"[^"]*"|//[^\n]*')


class Blocks(NamedTuple):
    """Where the blocks of one construct start and where the construct ends."""

    starts: list[int]
    """Offset of the ``(`` opening each block, in file order."""

    end: int
    """Offset of the ``)`` closing the construct the blocks sit in."""


@lru_cache
def _scanner(keyword: str) -> re.Pattern[str]:
    """Match a block opening, group 1, or any other parenthesis."""
    return re.compile(rf"(\(\s*{re.escape(keyword)}\b)|[()]")


def _mask_hidden(text: str) -> str:
    """Blank every quoted string and comment, keeping every offset in place."""
    return _HIDDEN.sub(lambda hidden: " " * (hidden.end() - hidden.start()), text)


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
    for match in _scanner(keyword).finditer(_mask_hidden(text)):
        if match.group(1) is not None:
            if level == 1:
                starts.append(match.start())
            level += 1
        elif match.group() == "(":
            level += 1
        else:
            level -= 1
            if level == 0:
                return Blocks(starts=starts, end=match.start())
    raise ValueError(
        f"No ')' closes the construct holding the {keyword} blocks; "
        f"the text is truncated"
    )
