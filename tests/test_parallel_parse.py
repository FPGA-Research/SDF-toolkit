"""Tests for the chunked parallel parse path."""

import re
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context

import pytest
from conftest import DATA_DIR
from lark import LarkError

from sdf_toolkit.io import parse
from sdf_toolkit.parser.chunking import top_level_cell_starts
from sdf_toolkit.parser.parser import (
    default_workers,
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
def test_top_level_cell_starts_counts(text: str, expected: int):
    assert len(top_level_cell_starts(text)) == expected


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
