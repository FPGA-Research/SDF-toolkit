"""Typed view of the Yosys JSON netlist that Verilog annotation reads.

{func}`yosys_running` and {func}`run_yosys` run Yosys on the Verilog, and
{func}`parse_yosys_json` turns its ``write_json`` output into a
{class}`YosysDesign` of modules, ports, cells and nets.  Each object keeps the
{class}`SourceSpan` Yosys reports, and every text edit of
{mod}`sdf_toolkit.io.annotate` lands at a position derived from those spans.
{class}`AnnotationError` is defined here because this layer is the first to
raise it, for a span Yosys merged from several places or a port bit the
netlist does not have.
"""

import json
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from sdf_toolkit.errors import SDFToolkitError


class AnnotationError(SDFToolkitError, ValueError):
    """The SDF or the Verilog holds something annotation cannot express."""


class YosysError(SDFToolkitError, RuntimeError):
    """Yosys is not on PATH, or it exits non-zero on the Verilog."""


class PortDirection(StrEnum):
    """Port direction in a Yosys module."""

    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"


class YosysPortJson(TypedDict):
    """A module port; Yosys omits ``offset`` 0 and ``upto`` 0."""

    direction: Literal["input", "output", "inout"]
    bits: list[int | str]
    offset: NotRequired[int]
    upto: NotRequired[int]
    signed: NotRequired[int]


class YosysCellJson(TypedDict):
    """A cell instance."""

    hide_name: int
    type: str
    parameters: dict[str, str]
    attributes: dict[str, str]
    port_directions: NotRequired[dict[str, str]]
    connections: dict[str, list[int | str]]


class YosysNetJson(TypedDict):
    """A named net; Yosys omits ``offset`` 0 and ``upto`` 0."""

    hide_name: int
    bits: list[int | str]
    attributes: dict[str, str]
    offset: NotRequired[int]
    upto: NotRequired[int]
    signed: NotRequired[int]


class YosysModuleJson(TypedDict):
    """A module."""

    attributes: dict[str, str]
    ports: dict[str, YosysPortJson]
    cells: dict[str, YosysCellJson]
    netnames: dict[str, YosysNetJson]


class YosysJson(TypedDict):
    """The top level of ``write_json`` output."""

    creator: str
    modules: dict[str, YosysModuleJson]


@dataclass(frozen=True, slots=True)
class SourceSpan:
    """A Yosys ``src`` attribute: 1-based lines and byte columns, end exclusive."""

    path: Path
    start_line: int
    start_column: int
    end_line: int
    end_column: int

    @classmethod
    def parse(cls, text: str) -> "SourceSpan":
        """Parse ``file:L.C-L.C``.

        Raises
        ------
        AnnotationError
            If *text* is not one span, as for an object Yosys merged from
            several places (``a|b``).
        """
        path, _, span = text.rpartition(":")
        start, _, end = span.partition("-")
        start_line, _, start_column = start.partition(".")
        end_line, _, end_column = end.partition(".")
        numbers = (start_line, start_column, end_line, end_column)
        if "|" in text or not path or not all(n.isdigit() for n in numbers):
            msg = f"Yosys src attribute {text!r} is not one file:L.C-L.C span"
            raise AnnotationError(msg)
        return cls(
            path=Path(path),
            start_line=int(start_line),
            start_column=int(start_column),
            end_line=int(end_line),
            end_column=int(end_column),
        )

    def __str__(self) -> str:
        """Return the span as ``file:line``."""
        return f"{self.path}:{self.start_line}"


def _optional_src(attributes: Mapping[str, str]) -> SourceSpan | None:
    """Return the ``src`` span, or None for an object Yosys made without one."""
    return SourceSpan.parse(attributes["src"]) if "src" in attributes else None


@dataclass
class YosysPort:
    """A port in a Yosys module.

    ``bits`` runs from the least significant bit.  ``offset`` is the lowest
    declared index and ``upto`` marks an ascending ``[lo:hi]`` range.
    """

    name: str
    direction: PortDirection
    bits: list[int | str] = field(default_factory=list)
    offset: int = 0
    upto: bool = False

    def bit_position(self, index: int) -> int:
        """Return the position in ``bits`` of the declared bit *index*.

        Raises
        ------
        AnnotationError
            If the port has no bit *index*.
        """
        relative = index - self.offset
        width = len(self.bits)
        if not 0 <= relative < width:
            msg = f"Port {self.name} has no bit {index}"
            raise AnnotationError(msg)
        return width - 1 - relative if self.upto else relative

    def bit_index(self, position: int) -> int:
        """Return the declared index of the bit at *position* in ``bits``.

        Raises
        ------
        AnnotationError
            If ``bits`` has no *position*.
        """
        width = len(self.bits)
        if not 0 <= position < width:
            msg = f"Port {self.name} has no bit position {position}"
            raise AnnotationError(msg)
        return self.offset + (width - 1 - position if self.upto else position)


@dataclass
class YosysCell:
    """A cell instance in a Yosys module."""

    name: str
    cell_type: str
    connections: dict[str, list[int | str]] = field(default_factory=dict)
    src: SourceSpan | None = None


@dataclass(frozen=True)
class YosysNet:
    """A named net; ``hidden`` marks a name Yosys generated."""

    name: str
    bits: list[int | str]
    hidden: bool
    src: SourceSpan | None


@dataclass
class YosysModule:
    """A module parsed from Yosys JSON output."""

    name: str
    ports: dict[str, YosysPort] = field(default_factory=dict)
    cells: dict[str, YosysCell] = field(default_factory=dict)
    netnames: dict[str, YosysNet] = field(default_factory=dict)
    src: SourceSpan | None = None


@dataclass
class YosysDesign:
    """Top-level Yosys design containing multiple modules."""

    modules: dict[str, YosysModule] = field(default_factory=dict)


@contextmanager
def yosys_running(
    verilog_path: Path, *, library: Sequence[Path] = (), top: str | None = None
) -> Iterator[Callable[[], YosysJson]]:
    """Start Yosys on a Verilog file and yield a function that waits for its JSON.

    Each *library* file is read as blackbox cells, and *top* names the module
    the hierarchy is checked from.  A Yosys still running when the block exits
    is killed.

    Raises
    ------
    YosysError
        If Yosys is not on PATH.  The yielded function raises it when Yosys
        exits non-zero, and the message then carries its stderr.
    """
    # write_json rejects a module with an always block; proc -noopt lowers it
    # to hidden cells and leaves named cells and nets as written.
    views = "".join(f"read_verilog -lib {view.resolve()}; " for view in library)
    hierarchy = "" if top is None else f"hierarchy -top {top}; "
    script = (
        f"{views}read_verilog {verilog_path.resolve()}; {hierarchy}"
        "proc -noopt; write_json -"
    )
    cmd = ["yosys", "-q", "-p", script]
    # Files, because Yosys would stall on a full pipe until the caller reads it.
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            process = subprocess.Popen(cmd, stdout=stdout, stderr=stderr)  # noqa: S603
        except FileNotFoundError:
            msg = (
                "Yosys is required for Verilog annotation but was not found. "
                "Install it from https://github.com/YosysHQ/yosys"
            )
            raise YosysError(msg) from None

        def result() -> YosysJson:
            returncode = process.wait()
            if returncode != 0:
                stderr.seek(0)
                text = stderr.read().decode(errors="replace")
                msg = f"Yosys failed (exit {returncode}):\n{text}"
                raise YosysError(msg)
            stdout.seek(0)
            data: YosysJson = json.load(stdout)
            return data

        try:
            yield result
        finally:
            process.kill()
            process.wait()


def run_yosys(verilog_path: Path) -> YosysJson:
    """Parse a Verilog file with Yosys and return its ``write_json`` output.

    Raises
    ------
    YosysError
        If Yosys is not on PATH, or exits non-zero; the message then carries
        its stderr.
    """
    with yosys_running(verilog_path) as result:
        return result()


def parse_yosys_json(json_data: YosysJson) -> YosysDesign:
    """Build a YosysDesign from ``write_json`` output.

    Raises
    ------
    KeyError
        If a field ``write_json`` always writes is missing.
    """
    design = YosysDesign()
    for mod_name, mod_data in json_data["modules"].items():
        design.modules[mod_name] = YosysModule(
            name=mod_name,
            ports={
                name: YosysPort(
                    name=name,
                    direction=PortDirection(port["direction"]),
                    bits=port["bits"],
                    offset=port.get("offset", 0),
                    upto=port.get("upto", 0) == 1,
                )
                for name, port in mod_data["ports"].items()
            },
            cells={
                name: YosysCell(
                    name=name,
                    cell_type=cell["type"],
                    connections=cell["connections"],
                    src=_optional_src(cell["attributes"]),
                )
                for name, cell in mod_data["cells"].items()
            },
            netnames={
                name: YosysNet(
                    name=name,
                    bits=net["bits"],
                    hidden=net["hide_name"] == 1,
                    src=_optional_src(net["attributes"]),
                )
                for name, net in mod_data["netnames"].items()
            },
            src=_optional_src(mod_data["attributes"]),
        )
    return design
