"""Split an SDF file into independently parsable chunks of cell blocks.

The body of a DELAYFILE is a flat sequence of ``(CELL ...)`` blocks, so a
large file can be cut at top-level cell boundaries and the pieces parsed
separately.  This module finds those boundaries; :mod:`sdf_toolkit.parser.parser`
turns them into chunks and merges the results.
"""

import re

# Only a quoted string or a comment can hide a parenthesis from the depth
# count: the grammar's unquoted STRING terminal cannot contain one.
_HIDDEN = re.compile(r'"[^"]*"|//[^\n]*')
# "(CELL" but not "(CELLTYPE"; the grammar ignores whitespace after "(".
_CELL_START = re.compile(r"\(\s*CELL\b")


def top_level_cell_starts(text: str) -> list[int]:
    """Return the offset of every ``(`` that opens a cell inside the DELAYFILE.

    Parameters
    ----------
    text : str
        The raw SDF file content.

    Returns
    -------
    list[int]
        Offsets in *text*, in file order.  A ``(CELL`` nested deeper than the
        DELAYFILE, or hidden inside a quoted string or a comment, is skipped.

    Examples
    --------
    >>> top_level_cell_starts('(DELAYFILE (CELL (CELLTYPE "A") (INSTANCE i)))')
    [11]
    >>> top_level_cell_starts('(DELAYFILE (CELL (CELLTYPE "(CELL x)")))')
    [11]
    """
    candidates = [match.start() for match in _CELL_START.finditer(text)]
    if not candidates:
        return []

    segments: list[tuple[int, int]] = []
    visible_from = 0
    for hidden in _HIDDEN.finditer(text):
        if hidden.start() > visible_from:
            segments.append((visible_from, hidden.start()))
        visible_from = hidden.end()
    segments.append((visible_from, len(text)))

    starts: list[int] = []
    depth = 0
    index = 0
    for seg_start, seg_end in segments:
        # Candidates before the segment sit inside a string or a comment.
        while index < len(candidates) and candidates[index] < seg_start:
            index += 1
        while index < len(candidates) and candidates[index] < seg_end:
            offset = candidates[index]
            at = (
                depth
                + text.count("(", seg_start, offset)
                - text.count(")", seg_start, offset)
            )
            if at == 1:
                starts.append(offset)
            index += 1
        depth += text.count("(", seg_start, seg_end) - text.count(
            ")", seg_start, seg_end
        )
    return starts
