"""Parse and emit every SDF file in tests/data against its golden file by name."""

from pathlib import Path

import pytest
from conftest import DATA_DIR

from sdf_toolkit.io.writer import emit_sdf
from sdf_toolkit.parser.parser import parse_sdf

GOLDEN_DIR = DATA_DIR / "golden"
SDF_FILES = sorted(DATA_DIR.glob("*.sdf"))


def test_every_input_has_a_golden() -> None:
    golden = {p.name for p in GOLDEN_DIR.glob("*.sdf")}
    assert {p.name for p in SDF_FILES} == golden


@pytest.mark.parametrize("sdf_path", SDF_FILES, ids=lambda p: p.name)
def test_emit_matches_golden(sdf_path: Path) -> None:
    emitted = emit_sdf(parse_sdf(sdf_path.read_text()))
    assert emitted == (GOLDEN_DIR / sdf_path.name).read_text()


@pytest.mark.parametrize("sdf_path", SDF_FILES, ids=lambda p: p.name)
def test_emit_then_parse_gives_equal_model(sdf_path: Path) -> None:
    parsed = parse_sdf(sdf_path.read_text())
    assert parse_sdf(emit_sdf(parsed)) == parsed
