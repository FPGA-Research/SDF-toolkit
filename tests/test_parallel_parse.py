"""Tests for the chunked parallel parse path."""

import gc
import re
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

import pytest
from conftest import DATA_DIR
from lark import LarkError

from sdf_toolkit.io import parse
from sdf_toolkit.parser.chunking import find_blocks
from sdf_toolkit.parser.grammar import HIDING_TERMINALS, hidden_patterns
from sdf_toolkit.parser.parser import (
    default_workers,
    get_parser,
    parallel_available,
    parse_sdf,
    parse_sdf_file,
)

HEADER = """(DELAYFILE
  (SDFVERSION "3.0")
  (DESIGN "top")
  (DIVIDER /)
  (TIMESCALE 1.0 ns)
"""


def _cell(celltype: str, instance: str, port: str = "A") -> str:
    return f"""  (CELL
    (CELLTYPE "{celltype}")
    (INSTANCE {instance})
    (DELAY (ABSOLUTE
      (PORT {port} (0.1:0.2:0.3))
      (IOPATH {port} Y (0.4:0.5:0.6) (0.7:0.8:0.9))
    ))
  )
"""


def _sdf(cells: str) -> str:
    return f"{HEADER}{cells})\n"


MANY_CELLS = _sdf("".join(_cell("BUF", f"inst_{n}") for n in range(40)))
# The same cell type and instance twice, so the second block's entry names
# collide with the first's across a chunk boundary.
REPEATED_INSTANCE = _sdf(
    _cell("BUF", "dup") + _cell("AND2", "other") + _cell("BUF", "dup")
)
QUOTED_PAREN = _sdf(
    '  (CELL (CELLTYPE "a)b (CELL x") (INSTANCE i1))\n' + _cell("BUF", "i2")
)
COMMENTED_CELL = _sdf(
    _cell("BUF", "i1")
    + "// (CELL (CELLTYPE nope) (INSTANCE nope)\n"
    + _cell("BUF", "i2")
)
NO_TIMING_LIST = _sdf(
    '  (CELL (CELLTYPE "INV_BA") (INSTANCE AFLSDF_INV_738))\n' + _cell("BUF", "i2")
)
# A header item after the cells: the chunk that holds it has to win over the
# prefix the header was read from.
HEADER_AFTER_CELLS = _sdf(
    _cell("BUF", "i1") + _cell("BUF", "i2") + '  (DESIGN "rewritten")\n'
)
ONE_CELL = _sdf(_cell("BUF", "only"))
NO_CELLS = _sdf("")

CASES = {
    "many_cells": MANY_CELLS,
    "repeated_instance": REPEATED_INSTANCE,
    "quoted_paren": QUOTED_PAREN,
    "commented_cell": COMMENTED_CELL,
    "no_timing_list": NO_TIMING_LIST,
    "header_after_cells": HEADER_AFTER_CELLS,
    "one_cell": ONE_CELL,
    "no_cells": NO_CELLS,
}


def _flatten(text: str, *, workers: int) -> list[tuple[str, str, str, tuple]]:
    """Parse and flatten to an ordered list, so key order is compared too."""
    sdf = parse_sdf(text, workers=workers)
    rows = []
    for celltype, instances in sdf.cells.items():
        for instance, entries in instances.items():
            for name, entry in entries.items():
                paths = entry.delay_paths
                triples = (
                    tuple(
                        (
                            field,
                            None
                            if values is None
                            else (values.min, values.avg, values.max),
                        )
                        for field, values in vars(paths).items()
                    )
                    if paths is not None
                    else ()
                )
                rows.append((celltype, instance, name, triples))
    return rows


@pytest.mark.parametrize("case", CASES.values(), ids=list(CASES))
@pytest.mark.parametrize("workers", [2, 3, 8])
def test_parallel_matches_serial(case: str, workers: int):
    assert _flatten(case, workers=workers) == _flatten(case, workers=1)


@pytest.mark.parametrize("case", CASES.values(), ids=list(CASES))
@pytest.mark.parametrize("workers", [2, 3, 8])
def test_parallel_header_matches_serial(case: str, workers: int):
    assert parse_sdf(case, workers=workers).header == parse_sdf(case, workers=1).header


def test_header_item_after_cells_wins():
    """A DESIGN written after the cells overrides the one in the prefix."""
    assert parse_sdf(HEADER_AFTER_CELLS, workers=2).header.design == "rewritten"


def test_cross_chunk_name_collision_suffixes():
    """The second block of a repeated instance continues the _1 suffixing."""
    entries = parse_sdf(REPEATED_INSTANCE, workers=2).cells["BUF"]["dup"]
    assert sorted(entries) == ["iopath_A_Y", "iopath_A_Y_1", "port_A", "port_A_1"]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (NO_CELLS, 0),
        (ONE_CELL, 1),
        (REPEATED_INSTANCE, 3),
        (QUOTED_PAREN, 2),
        (COMMENTED_CELL, 2),
        (MANY_CELLS, 40),
        (HEADER_AFTER_CELLS, 2),
    ],
)
def test_find_blocks_counts_cells(text: str, expected: int):
    assert len(find_blocks(text, keyword="CELL").starts) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [(NO_CELLS, 0), (ONE_CELL, 1), (MANY_CELLS, 40), (COMMENTED_CELL, 2)],
)
def test_find_blocks_ends_at_the_delayfile_close(text: str, expected: int):
    """The reported end is the file's own last parenthesis."""
    assert find_blocks(text, keyword="CELL").end == text.rindex(")")
    assert len(find_blocks(text, keyword="CELL").starts) == expected


@pytest.mark.parametrize(("keyword", "expected"), [("IOPATH", 1), ("PORT", 1)])
def test_find_blocks_splits_the_entries_of_one_cell(keyword: str, expected: int):
    """The same scan cuts the entry list of a cell, not just a file's cells."""
    cell = ONE_CELL[find_blocks(ONE_CELL, keyword="CELL").starts[0] :]
    absolute = cell[cell.index("(ABSOLUTE") :]
    entries = find_blocks(absolute, keyword=keyword)
    assert len(entries.starts) == expected
    assert all(absolute[at:].startswith(f"({keyword}") for at in entries.starts)


def test_find_blocks_rejects_a_truncated_construct():
    with pytest.raises(ValueError, match="truncated"):
        find_blocks('(DELAYFILE (CELL (CELLTYPE "A")', keyword="CELL")


@pytest.mark.parametrize("workers", [1, 2, 8])
def test_truncated_file_raises_the_same_class_at_every_worker_count(workers: int):
    """Whether the split runs must not decide which error class is raised."""
    with pytest.raises(LarkError):
        parse_sdf(MANY_CELLS.rstrip()[:-1], workers=workers)


@pytest.mark.parametrize("pattern", hidden_patterns())
def test_hidden_patterns_never_start_with_a_parenthesis(pattern: str):
    """The scan reads a match's first character to tell hidden from structure."""
    for probe in ["(", "()", "(CELL x)", "(// x", '("a")']:
        assert re.compile(pattern).match(probe) is None


def test_hidden_patterns_come_from_the_grammar():
    """The scan reads the grammar's terminals rather than a copy of them."""
    terminals = {
        terminal.name: terminal.pattern.to_regexp()
        for terminal in get_parser().parser.terminals
    }
    assert hidden_patterns() == tuple(terminals[name] for name in HIDING_TERMINALS)


@pytest.mark.parametrize(
    "sdf_path", sorted(DATA_DIR.glob("*.sdf")), ids=lambda p: p.name
)
def test_data_files_parse_identically_in_parallel(sdf_path):
    text = sdf_path.read_text()
    assert parse_sdf(text, workers=4) == parse_sdf(text, workers=1)


def test_parse_sdf_file_takes_workers(tmp_path):
    path = tmp_path / "many.sdf"
    path.write_text(MANY_CELLS)
    assert parse_sdf_file(path, workers=4) == parse_sdf(MANY_CELLS, workers=1)


def test_io_parse_takes_workers():
    assert parse(MANY_CELLS, workers=4) == parse(MANY_CELLS, workers=1)


@pytest.mark.parametrize("workers", [0, -1])
def test_workers_below_one_raises(workers: int):
    with pytest.raises(ValueError, match="workers must be at least 1"):
        parse_sdf(MANY_CELLS, workers=workers)


@pytest.mark.parametrize(
    ("length", "expected_serial"),
    [(0, True), (4_000_000 - 1, True), (4_000_000, False)],
)
def test_default_workers_threshold(length: int, expected_serial: bool):
    assert (default_workers(length) == 1) is expected_serial


def _parse_in_child(text: str) -> int:
    """Parse with parallelism requested, from inside a worker process."""
    return len(parse_sdf(text, workers=4).cells)


def test_parse_from_inside_a_process_pool_worker():
    """A nested pool must not hang: the child is non-daemonic, so it forks."""
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("fork")) as pool:
        assert list(pool.map(_parse_in_child, [MANY_CELLS, MANY_CELLS])) == [1, 1]


def test_parse_from_inside_a_daemonic_worker():
    """multiprocessing.Pool workers are daemonic and may not have children."""
    with get_context("fork").Pool(processes=1) as pool:
        assert pool.apply(_parse_in_child, (MANY_CELLS,)) == 1


def test_parallel_available_is_true_in_the_test_process():
    assert parallel_available() is True


@pytest.mark.parametrize("workers", [2, 3, 8])
def test_error_line_is_reported_in_file_lines(workers: int):
    """A chunk reports the line of the whole file, not of its own text."""
    broken = MANY_CELLS.replace("(PORT A", "(PORT @", 1)
    with pytest.raises(LarkError) as serial:
        parse_sdf(broken, workers=1)
    with pytest.raises(LarkError) as parallel:
        parse_sdf(broken, workers=workers)
    assert _error_position(parallel.value) == _error_position(serial.value)


def _error_position(error: LarkError) -> str:
    match = re.search(r"failed at (\d+:\d+)", str(error))
    assert match is not None, str(error)
    return match.group(1)


@pytest.mark.parametrize("workers", [1, 4])
@pytest.mark.parametrize("enabled_before", [True, False])
def test_parse_leaves_the_collector_as_it_found_it(workers: int, enabled_before: bool):
    """The parse holds cyclic collection off, and hands back the prior state."""
    was_enabled = gc.isenabled()
    try:
        gc.enable() if enabled_before else gc.disable()
        parse_sdf(MANY_CELLS, workers=workers)
        assert gc.isenabled() is enabled_before
    finally:
        gc.enable() if was_enabled else gc.disable()


def test_parse_re_enables_the_collector_after_a_failure():
    """A syntax error must not leave collection off for the whole process."""
    was_enabled = gc.isenabled()
    gc.enable()
    try:
        with pytest.raises(LarkError):
            parse_sdf("(DELAYFILE (CELL (CELLTYPE nope))", workers=1)
        assert gc.isenabled() is True
    finally:
        gc.enable() if was_enabled else gc.disable()
