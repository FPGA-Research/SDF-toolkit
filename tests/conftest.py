"""Shared test constants and fixtures."""

from pathlib import Path

import pytest

from sdf_toolkit.core.pathgraph import TimingGraph
from sdf_toolkit.parser.parser import parse_sdf

DATA_DIR = (Path(__file__).parent / "data").resolve()


@pytest.fixture
def spec1_graph() -> TimingGraph:
    """Build a TimingGraph from the spec-example1.sdf test fixture."""
    sdf_content = (DATA_DIR / "spec-example1.sdf").read_text()
    sdf = parse_sdf(sdf_content)
    return TimingGraph(sdf)


@pytest.fixture
def spec2_graph() -> TimingGraph:
    """Build a TimingGraph from spec-example2.sdf, which has no loop."""
    return TimingGraph(parse_sdf((DATA_DIR / "spec-example2.sdf").read_text()))
