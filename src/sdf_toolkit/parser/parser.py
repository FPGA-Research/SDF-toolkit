"""Lark-based SDF file parser with thread-safe caching.

Two things make a large file parse quickly.  The transformer runs inside the
LALR parser, so no intermediate tree is ever built, and a file big enough to
pay for it is cut at top-level cell boundaries and its chunks parsed in
worker processes.  The grammar carries a start rule per fragment, ``head``
for a file cut short before its first cell and ``body`` for a run of items
between two cells, so a chunk is parsed as the fragment it is.
"""

import gc
import os
import threading
from collections.abc import Iterator
from concurrent.futures import ProcessPoolExecutor
from contextlib import contextmanager
from multiprocessing import current_process, get_all_start_methods, get_context
from pathlib import Path
from typing import NamedTuple, cast

from lark import Lark, LarkError, UnexpectedInput

from sdf_toolkit.core.model import SDFFile
from sdf_toolkit.parser.chunking import find_blocks
from sdf_toolkit.parser.grammar import START_RULES, load_grammar
from sdf_toolkit.parser.transformers import (
    CellBlock,
    ParsedChunk,
    SDFBlockTransformer,
    SDFTransformer,
    apply_header,
    assemble_cells,
)

#: Smallest input ``parse_sdf`` splits across processes on its own.
PARALLEL_MIN_BYTES = 4_000_000
#: Upper bound on the worker count ``parse_sdf`` picks on its own.
MAX_AUTO_WORKERS = 8
#: Keyword of the blocks a file is split between.
CELL_KEYWORD = "CELL"
#: Chunks per worker.  Smaller chunks overlap unpickling with parsing:
#: 6.8 s at 4 against 8.4 s at 1 on a 28 MB back-annotated file.
CHUNKS_PER_WORKER = 4


def _build_lark(transformer: SDFBlockTransformer) -> Lark:
    """Build a LALR parser that runs *transformer* as it reduces.

    Binding the transformer here skips the parse tree entirely, which is
    where most of the time on a large file used to go.  The instance is
    bound once and keeps state between rules, so every caller of this
    function has to reset it before each parse.
    """
    return Lark(
        load_grammar(),
        parser="lalr",
        start=list(START_RULES),
        transformer=transformer,
    )


class SDFLarkParser:
    """Lark-based SDF parser that replaces the PLY implementation."""

    def __init__(self) -> None:
        """Initialize the parser with the SDF grammar."""
        self.transformer = SDFTransformer()
        self.parser = _build_lark(self.transformer)

    def parse(self, input_text: str) -> SDFFile:
        """Parse SDF input text and return an SDFFile."""
        self.transformer.reset()
        try:
            return cast("SDFFile", self.parser.parse(input_text, start="start"))
        except LarkError as e:
            raise LarkError(
                f"SDF parsing failed at {getattr(e, 'line', 'unknown')}:"
                f"{getattr(e, 'column', 'unknown')} - {e!s}"
            ) from e
        except Exception as e:
            raise type(e)(f"Unexpected error during SDF parsing: {e!s}") from e

    def parse_file(self, filepath: Path | str) -> SDFFile:
        """Read and parse an SDF file from disk."""
        return self.parse(_read_sdf(filepath))


class SDFChunkParser:
    """Parser for one fragment of an SDF file, stopping at the cell blocks.

    The grammar has a start rule per fragment, so a fragment is parsed as
    what it is rather than padded back into a whole file.
    """

    def __init__(self) -> None:
        """Initialize the parser with the SDF grammar."""
        self.transformer = SDFBlockTransformer()
        self.parser = _build_lark(self.transformer)

    def parse_head(self, input_text: str) -> ParsedChunk:
        """Parse a file cut short before its first cell."""
        self.transformer.reset()
        return cast("ParsedChunk", self.parser.parse(input_text, start="head"))

    def parse_body(self, input_text: str) -> ParsedChunk:
        """Parse a run of whole items cut out of a file."""
        self.transformer.reset()
        return cast("ParsedChunk", self.parser.parse(input_text, start="body"))


_local = threading.local()


def get_parser() -> SDFLarkParser:
    """Get or create a thread-local parser instance."""
    if not hasattr(_local, "parser"):
        _local.parser = SDFLarkParser()
    return _local.parser


def _get_chunk_parser() -> SDFChunkParser:
    """Get or create a thread-local chunk parser instance."""
    if not hasattr(_local, "chunk_parser"):
        _local.chunk_parser = SDFChunkParser()
    return _local.chunk_parser


def _read_sdf(filepath: Path | str) -> str:
    """Read an SDF file from disk."""
    try:
        return Path(filepath).read_text()
    except OSError as e:
        raise OSError(f"Error reading SDF file {filepath}: {e!s}") from e


class _ChunkJob(NamedTuple):
    """One chunk of SDF text and the file line its body starts on."""

    text: str
    first_line: int


def _parse_chunk(job: _ChunkJob) -> ParsedChunk:
    """Parse one chunk, reporting any position in lines of the whole file."""
    try:
        return _get_chunk_parser().parse_body(job.text)
    except UnexpectedInput as e:
        raise LarkError(
            f"SDF parsing failed at {e.line + job.first_line - 1}:{e.column} - {e!s}"
        ) from e


@contextmanager
def _deferred_cyclic_gc() -> Iterator[None]:
    """Hold the cyclic collector off for the duration of a parse.

    A parse builds millions of model objects that reference each other as a
    tree, so reference counting frees all but a few thousand of them and
    every generation pass the allocations trigger walks the heap for nothing:
    5.2 s against 6.0 s on a 28 MB file, at the same peak RSS.  The cycles
    Lark does leave behind are collected whenever the collector next runs.

    The collector is process-wide, so a parse in another thread is held off
    too, and a worker forked inside the block inherits the state.  A caller
    that had already disabled collection keeps it disabled.
    """
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


def parallel_available() -> bool:
    """Report whether this process may start the workers a split needs.

    Returns
    -------
    bool
        False in a daemonic process, which the standard library forbids from
        having children, and on a platform without the fork start method.
        ``parse_sdf`` parses serially in both cases.
    """
    return not current_process().daemon and "fork" in get_all_start_methods()


def default_workers(text_length: int) -> int:
    """Return the worker count ``parse_sdf`` uses for an input of this size.

    Parameters
    ----------
    text_length : int
        Length of the SDF text in characters.

    Returns
    -------
    int
        1 for an input too small to pay for the split, otherwise the CPU
        count capped at {data}`MAX_AUTO_WORKERS`.
    """
    if text_length < PARALLEL_MIN_BYTES:
        return 1
    return min(os.cpu_count() or 1, MAX_AUTO_WORKERS)


def _parse_parallel(input_text: str, *, workers: int) -> SDFFile:
    """Parse the cell blocks of *input_text* in *workers* worker processes."""
    try:
        blocks_found = find_blocks(input_text, keyword=CELL_KEYWORD)
    except ValueError as e:
        # The serial path reports a truncated file as a parse error, and the
        # size of the file must not decide which class the caller catches.
        raise LarkError(f"SDF parsing failed: {e!s}") from e
    starts = blocks_found.starts
    chunks = min(workers * CHUNKS_PER_WORKER, len(starts))
    if chunks < 2:
        # Nothing to split: fewer than two cells in the file.
        return get_parser().parse(input_text)

    # A chunk runs from one cell block to the next, and the last one stops at
    # the parenthesis closing the file rather than swallowing it.
    stops = [*starts[1:], blocks_found.end]
    edges = sorted({len(starts) * n // chunks for n in range(chunks + 1)})
    jobs = [
        _ChunkJob(
            text=input_text[starts[first] : stops[last - 1]],
            first_line=input_text.count("\n", 0, starts[first]) + 1,
        )
        for first, last in zip(edges, edges[1:], strict=False)
    ]
    # The head holds the header items written before the first cell.
    header = _get_chunk_parser().parse_head(input_text[: starts[0]]).header
    with ProcessPoolExecutor(
        max_workers=workers, mp_context=get_context("fork")
    ) as pool:
        results = list(pool.map(_parse_chunk, jobs))

    blocks: list[CellBlock] = []
    for result in results:
        apply_header(header, result.header)
        blocks.extend(result.blocks)
    if len(blocks) != len(starts):
        raise LarkError(
            f"Chunked SDF parse read {len(blocks)} cells where the split found "
            f"{len(starts)}; the file was cut in the wrong place"
        )
    return SDFFile(header=header, cells=assemble_cells(blocks))


def parse_sdf(input_text: str, *, workers: int | None = None) -> SDFFile:
    """Parse SDF text using a thread-local Lark parser.

    Parameters
    ----------
    input_text : str
        The raw SDF file content.
    workers : int | None
        Number of worker processes to parse the cell blocks in.  The default
        is one per CPU up to {data}`MAX_AUTO_WORKERS` for an input of at
        least {data}`PARALLEL_MIN_BYTES`, and 1, meaning no worker process
        at all, for anything smaller.  Pass 1 to keep the parse in this
        process.

    Returns
    -------
    SDFFile
        The parsed SDF file object.

    Raises
    ------
    ValueError
        If *workers* is below 1.
    """
    count = default_workers(len(input_text)) if workers is None else workers
    if count < 1:
        raise ValueError(f"workers must be at least 1, got {count}")
    with _deferred_cyclic_gc():
        if count == 1 or not parallel_available():
            return get_parser().parse(input_text)
        return _parse_parallel(input_text, workers=count)


def parse_sdf_file(filepath: Path | str, *, workers: int | None = None) -> SDFFile:
    """Parse an SDF file from disk using a thread-local Lark parser.

    Parameters
    ----------
    filepath : Path | str
        Path of the SDF file to read.
    workers : int | None
        Number of worker processes, as in {func}`parse_sdf`.

    Returns
    -------
    SDFFile
        The parsed SDF file object.
    """
    return parse_sdf(_read_sdf(filepath), workers=workers)
