import json
import subprocess
import sys
from importlib.metadata import version
from pathlib import Path
from types import FunctionType
from unittest.mock import patch

import pytest
import typer
from conftest import DATA_DIR
from typer.testing import CliRunner

from sdf_toolkit import cli
from sdf_toolkit.cli import app, main

runner = CliRunner()

SPEC_EXAMPLE1 = str(DATA_DIR / "spec-example1.sdf")
EMPTY_SDF = str(DATA_DIR / "empty.sdf")


class TestParse:
    def test_parse_json(self) -> None:
        result = runner.invoke(app, ["parse", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "header" in data
        assert "cells" in data

    def test_parse_sdf(self) -> None:
        result = runner.invoke(app, ["parse", SPEC_EXAMPLE1, "--format", "sdf"])
        assert result.exit_code == 0
        assert "DELAYFILE" in result.output

    def test_parse_missing_file(self) -> None:
        result = runner.invoke(app, ["parse", "/nonexistent/file.sdf"])
        assert result.exit_code != 0


class TestEmit:
    def test_emit_roundtrip(self, tmp_path: Path) -> None:
        parse_result = runner.invoke(app, ["parse", SPEC_EXAMPLE1])
        assert parse_result.exit_code == 0

        json_file = tmp_path / "roundtrip.json"
        json_file.write_text(parse_result.output)

        emit_result = runner.invoke(app, ["emit", str(json_file)])
        assert emit_result.exit_code == 0
        assert "DELAYFILE" in emit_result.output


class TestInfo:
    def test_info(self) -> None:
        result = runner.invoke(app, ["info", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "SDF Header" in result.output
        assert "Cell Summary" in result.output


class TestCompose:
    def test_compose(self) -> None:
        result = runner.invoke(app, ["compose", SPEC_EXAMPLE1, "P1/z", "P2/i"])
        assert result.exit_code == 0
        assert "Path 1" in result.output

    def test_compose_verbose(self) -> None:
        result = runner.invoke(app, ["compose", SPEC_EXAMPLE1, "P1/z", "P2/i", "-v"])
        assert result.exit_code == 0
        assert "->" in result.output


class TestVerify:
    def test_verify_pass(self) -> None:
        expected = json.dumps(
            [
                {"min": 1.805, "typ": None, "max": 1.805},
                {"min": 1.795, "typ": None, "max": 1.795},
            ]
        )
        result = runner.invoke(
            app,
            ["verify", SPEC_EXAMPLE1, "P1/z", "P2/i", "--expected", expected],
        )
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_verify_fail(self) -> None:
        expected = json.dumps(
            [
                {"min": 999.0, "typ": None, "max": 999.0},
                {"min": 999.0, "typ": None, "max": 999.0},
            ]
        )
        result = runner.invoke(
            app,
            ["verify", SPEC_EXAMPLE1, "P1/z", "P2/i", "--expected", expected],
        )
        assert result.exit_code == 1
        assert "FAIL" in result.output


class TestDecompose:
    def test_decompose(self) -> None:
        total = json.dumps([{"min": 3.0, "typ": None, "max": 3.0}])
        known = json.dumps([{"min": 1.0, "typ": None, "max": 1.0}])
        result = runner.invoke(app, ["decompose", "--total", total, "--known", known])
        assert result.exit_code == 0
        assert json.loads(result.output) == [{"min": 2.0, "typ": None, "max": 2.0}]

    @pytest.mark.parametrize(
        "total",
        ['{"nominal": {"min": 3.0}}', "[]", '[{"min": 3.0, "avg": 1.0}]'],
        ids=["dict", "empty", "unknown-key"],
    )
    def test_decompose_rejects_bad_json(self, total: str) -> None:
        known = json.dumps([{"min": 1.0, "typ": None, "max": 1.0}])
        result = runner.invoke(app, ["decompose", "--total", total, "--known", known])
        assert result.exit_code == 2


class TestCriticalPathCmd:
    def test_critical_path(self) -> None:
        result = runner.invoke(app, ["critical-path", SPEC_EXAMPLE1, "P1/z", "P2/i"])
        assert result.exit_code == 0
        assert "Critical path scalar" in result.output
        assert "->" in result.output

    def test_critical_path_with_corner_and_edge(self) -> None:
        result = runner.invoke(
            app,
            [
                "critical-path",
                SPEC_EXAMPLE1,
                "P1/z",
                "P2/i",
                "--corner",
                "min",
                "--edge",
                "rise",
            ],
        )
        assert result.exit_code == 0
        assert "Critical path scalar" in result.output

    def test_critical_path_no_path(self) -> None:
        result = runner.invoke(app, ["critical-path", SPEC_EXAMPLE1, "P2/i", "P1/z"])
        assert result.exit_code == 1
        assert "No path found" in result.stderr


class TestRankPathsCmd:
    def test_rank_paths(self) -> None:
        result = runner.invoke(app, ["rank-paths", SPEC_EXAMPLE1, "P1/z", "P2/i"])
        assert result.exit_code == 0
        assert "#1" in result.output
        assert "#2" in result.output

    def test_rank_paths_limit(self) -> None:
        result = runner.invoke(
            app, ["rank-paths", SPEC_EXAMPLE1, "P1/z", "P2/i", "-n", "1"]
        )
        assert result.exit_code == 0
        assert "#1" in result.output
        assert "#2" not in result.output

    def test_rank_paths_ascending(self) -> None:
        result = runner.invoke(
            app, ["rank-paths", SPEC_EXAMPLE1, "P1/z", "P2/i", "--ascending"]
        )
        assert result.exit_code == 0

    def test_rank_paths_no_path(self) -> None:
        result = runner.invoke(app, ["rank-paths", SPEC_EXAMPLE1, "P2/i", "P1/z"])
        assert result.exit_code == 1
        assert "No paths found" in result.stderr


class TestSlackCmd:
    def test_slack_positive(self) -> None:
        result = runner.invoke(app, ["slack", SPEC_EXAMPLE1, "P1/z", "P2/i", "10.0"])
        assert result.exit_code == 0
        assert "Slack:" in result.output

    def test_slack_negative(self) -> None:
        result = runner.invoke(app, ["slack", SPEC_EXAMPLE1, "P1/z", "P2/i", "0.5"])
        assert result.exit_code == 1
        assert "TIMING VIOLATION" in result.stdout

    def test_slack_no_path(self) -> None:
        result = runner.invoke(app, ["slack", SPEC_EXAMPLE1, "P2/i", "P1/z", "10.0"])
        assert result.exit_code == 1


class TestDotCmd:
    def test_dot_stdout(self) -> None:
        result = runner.invoke(app, ["dot", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "digraph timing" in result.output
        assert "rankdir=LR" in result.output

    def test_dot_cluster(self) -> None:
        result = runner.invoke(app, ["dot", SPEC_EXAMPLE1, "--cluster"])
        assert result.exit_code == 0
        assert "subgraph cluster_" in result.output

    def test_dot_highlight(self) -> None:
        result = runner.invoke(
            app,
            [
                "dot",
                SPEC_EXAMPLE1,
                "--highlight-source",
                "P1/z",
                "--highlight-sink",
                "P2/i",
            ],
        )
        assert result.exit_code == 0
        assert 'color="red"' in result.output

    def test_dot_output_file(self, tmp_path: Path) -> None:
        out = tmp_path / "graph.dot"
        result = runner.invoke(app, ["dot", SPEC_EXAMPLE1, "-o", str(out)])
        assert result.exit_code == 0
        assert result.stdout == ""
        assert f"Written to {out}" in result.stderr
        content = out.read_text()
        assert "digraph timing" in content


TEST1 = str(DATA_DIR / "test1.sdf")


class TestNormalizeCmd:
    def test_normalize_json(self) -> None:
        result = runner.invoke(app, ["normalize", SPEC_EXAMPLE1, "--target", "1ps"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "header" in data
        assert data["header"]["timescale"] == "1ps"

    def test_normalize_sdf(self) -> None:
        result = runner.invoke(
            app, ["normalize", SPEC_EXAMPLE1, "--target", "1ps", "-f", "sdf"]
        )
        assert result.exit_code == 0
        assert "DELAYFILE" in result.output


class TestLintCmd:
    def test_lint_clean_file(self) -> None:
        result = runner.invoke(app, ["lint", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "No issues found." in result.stderr

    def test_lint_with_issues(self) -> None:
        """Lint a file that has issues to cover the table output path."""
        result = runner.invoke(app, ["lint", EMPTY_SDF])
        assert result.exit_code == 1
        assert "Lint Issues" in result.stdout

    @pytest.mark.parametrize(
        ("severity", "exit_code"),
        [("error", 0), ("ERROR", 0), ("warning", 1)],
    )
    def test_lint_severity_filter(self, severity: str, exit_code: int) -> None:
        # empty.sdf has only warnings.
        result = runner.invoke(app, ["lint", EMPTY_SDF, "--severity", severity])
        assert result.exit_code == exit_code, result.output


class TestStatsCmd:
    def test_stats_default(self) -> None:
        result = runner.invoke(app, ["stats", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "SDF Statistics" in result.output

    def test_stats_custom_corner_edge(self) -> None:
        result = runner.invoke(
            app, ["stats", TEST1, "--corner", "MIN", "--edge", "fall"]
        )
        assert result.exit_code == 0
        assert "SDF Statistics" in result.output
        assert "Entry Type Counts" in result.output


class TestQueryCmd:
    def test_query_json(self) -> None:
        result = runner.invoke(app, ["query", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "cells" in data

    def test_query_sdf(self) -> None:
        result = runner.invoke(app, ["query", SPEC_EXAMPLE1, "-f", "sdf"])
        assert result.exit_code == 0
        assert "DELAYFILE" in result.output

    def test_query_cell_type_filter(self) -> None:
        result = runner.invoke(app, ["query", SPEC_EXAMPLE1, "--cell-type", "INV"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "INV" in data["cells"]

    def test_query_entry_type_filter(self) -> None:
        result = runner.invoke(app, ["query", SPEC_EXAMPLE1, "--entry-type", "iopath"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "cells" in data

    def test_query_entry_type_uppercase(self) -> None:
        # The natural SDF spelling is upper case; it must not crash.
        result = runner.invoke(app, ["query", SPEC_EXAMPLE1, "--entry-type", "IOPATH"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "cells" in data

    def test_query_entry_type_invalid_is_clean_error(self) -> None:
        result = runner.invoke(app, ["query", SPEC_EXAMPLE1, "--entry-type", "bogus"])
        assert result.exit_code != 0
        # A clean parameter error, not a raw traceback.
        assert "Traceback" not in result.output


class TestDiffCmd:
    def test_diff_identical(self) -> None:
        result = runner.invoke(app, ["diff", SPEC_EXAMPLE1, SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "identical" in result.stderr.lower()

    def test_diff_different_files(self) -> None:
        result = runner.invoke(app, ["diff", TEST1, SPEC_EXAMPLE1, "--normalize"])
        assert result.exit_code == 1
        # Different timescales produce header diffs and/or entry diffs
        output = result.output
        assert (
            "Header Differences" in output
            or "Only in A" in output
            or "Only in B" in output
            or "Value Differences" in output
        )

    def test_diff_value_diffs(self, tmp_path: Path) -> None:
        """Diff two files with same structure but different delay values."""
        original = Path(SPEC_EXAMPLE1).read_text()
        # Replace a delay value that exists in the file
        modified = original.replace(".345", ".999")
        modified_file = tmp_path / "modified.sdf"
        modified_file.write_text(modified)

        result = runner.invoke(app, ["diff", SPEC_EXAMPLE1, str(modified_file)])
        assert result.exit_code == 1
        assert "Value Differences" in result.output


class TestMergeCmd:
    def test_merge_json(self) -> None:
        result = runner.invoke(app, ["merge", SPEC_EXAMPLE1, SPEC_EXAMPLE1])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "cells" in data

    def test_merge_sdf(self) -> None:
        result = runner.invoke(
            app, ["merge", SPEC_EXAMPLE1, SPEC_EXAMPLE1, "-f", "sdf"]
        )
        assert result.exit_code == 0
        assert "DELAYFILE" in result.output

    def test_merge_strategy(self) -> None:
        result = runner.invoke(
            app,
            ["merge", SPEC_EXAMPLE1, SPEC_EXAMPLE1, "--strategy", "keep-first"],
        )
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert "cells" in data


class TestBatchAnalysisCmd:
    def test_batch_analysis_default(self) -> None:
        result = runner.invoke(app, ["batch-analysis", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "Batch Endpoint Analysis" in result.output

    def test_batch_analysis_with_limit(self) -> None:
        result = runner.invoke(app, ["batch-analysis", SPEC_EXAMPLE1, "-n", "2"])
        assert result.exit_code == 0
        assert "Batch Endpoint Analysis" in result.output

    def test_batch_analysis_no_endpoints(self) -> None:
        """Empty SDF graph has no endpoint pairs."""
        result = runner.invoke(app, ["batch-analysis", EMPTY_SDF])
        assert result.exit_code == 1
        assert "No endpoint pairs found" in result.stderr


class TestReportCmd:
    def test_report_default(self) -> None:
        result = runner.invoke(app, ["report", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "SDF Header" in result.output or len(result.output) > 0

    def test_report_with_period(self) -> None:
        result = runner.invoke(app, ["report", SPEC_EXAMPLE1, "--period", "10.0"])
        assert result.exit_code == 0


class TestMainEntry:
    def test_main_callable(self) -> None:
        """Test that main() is importable and the app runs with --help."""
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        assert "Usage" in result.output

    def test_main_function_directly(self) -> None:
        """Call main() directly to cover the entry point."""
        with patch("sdf_toolkit.cli.app") as mock_app:
            main()
            mock_app.assert_called_once()

    def test_python_m_sdf_toolkit(self) -> None:
        """Test running as ``python -m sdf_toolkit --help``."""
        proc = subprocess.run(
            [sys.executable, "-m", "sdf_toolkit", "--help"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0
        assert "Usage" in proc.stdout


class TestNoArgs:
    def test_no_args_shows_help(self) -> None:
        result = runner.invoke(app, [])
        assert "Usage" in result.output


GOLDEN_DIR = DATA_DIR / "golden"


@pytest.mark.parametrize(
    ("args", "exit_code", "message"),
    [
        (["lint", SPEC_EXAMPLE1, "--severity", "errr"], 2, "--severity"),
        (["merge", SPEC_EXAMPLE1], 2, "at least 2 files"),
        (["merge", SPEC_EXAMPLE1, SPEC_EXAMPLE1, "--strategy", "bogus"], 2, "bogus"),
        (["parse", "/nonexistent/file.sdf"], 2, "does not exist"),
        (["parse", "."], 2, "is a directory"),
        (["compose", SPEC_EXAMPLE1, "nope", "P2/i"], 2, "'nope'"),
        (["rank-paths", SPEC_EXAMPLE1, "P1/z", "nope"], 2, "'nope'"),
        (["dot", SPEC_EXAMPLE1, "--highlight-source", "P1/z"], 2, "together"),
        (
            ["dot", SPEC_EXAMPLE1, "--highlight-source", "P2/i"]
            + ["--highlight-sink", "P1/z"],
            1,
            "No path from P2/i to P1/z",
        ),
        (["critical-path", SPEC_EXAMPLE1, "P2/i", "P1/z"], 1, "No path found"),
    ],
    ids=[
        "lint-bad-severity",
        "merge-one-file",
        "merge-bad-strategy",
        "missing-file",
        "directory",
        "unknown-source-pin",
        "unknown-sink-pin",
        "dot-half-highlight",
        "dot-highlight-no-path",
        "critical-path-no-path",
    ],
)
def test_error_goes_to_stderr(args: list[str], exit_code: int, message: str) -> None:
    result = runner.invoke(app, args)
    assert result.exit_code == exit_code, result.output
    assert result.stdout == ""
    # Typer forces rich styling when GITHUB_ACTIONS is set, which splits option names.
    assert message in typer.unstyle(result.stderr)
    assert "Traceback" not in result.output


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"cells": {}}, "'header'"),
        ({"header": {}, "cells": {"INV": {"u1": [{"mode": "absolute"}]}}}, "'type'"),
        ([], "is not SDF JSON"),
    ],
    ids=["no-header", "entry-without-type", "not-an-object"],
)
def test_emit_rejects_incomplete_json(
    tmp_path: Path, data: object, message: str
) -> None:
    json_file = tmp_path / "bad.json"
    json_file.write_text(json.dumps(data))
    result = runner.invoke(app, ["emit", str(json_file)])
    assert result.exit_code == 1, result.output
    assert message in result.stderr


@pytest.mark.parametrize("name", ["modulo6.sdf", "spec-example2.sdf"])
def test_parse_then_emit_matches_golden(tmp_path: Path, name: str) -> None:
    parsed = runner.invoke(app, ["parse", str(DATA_DIR / name)])
    assert parsed.exit_code == 0, parsed.output
    json_file = tmp_path / "parsed.json"
    json_file.write_text(parsed.stdout)
    emitted = runner.invoke(app, ["emit", str(json_file)])
    assert emitted.exit_code == 0, emitted.output
    assert emitted.stdout == (GOLDEN_DIR / name).read_text()


def test_port_to_interconnect_unresolved_driver_exits_1(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A netlist with no instances leaves every PORT load without a driver.
    monkeypatch.setattr(cli, "run_yosys", lambda _path: {"modules": {"port_top": {}}})
    result = runner.invoke(
        app,
        [
            "port-to-interconnect",
            str(DATA_DIR / "port_netlist.sdf"),
            str(DATA_DIR / "port_netlist.v"),
        ],
    )
    assert result.exit_code == 1, result.output
    assert "could not resolve a unique driver" in result.stderr


def test_version() -> None:
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0, result.output
    assert result.stdout == f"sdf-toolkit {version('sdf-toolkit')}\n"


def _command_names() -> list[str]:
    # Typer names a command without name= after its function, "_" as "-".
    names = []
    for cmd in app.registered_commands:
        assert isinstance(cmd.callback, FunctionType)
        names.append(cmd.name or cmd.callback.__name__.replace("_", "-"))
    return sorted(names)


@pytest.mark.parametrize("command", _command_names())
def test_help_has_no_rst_markup(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0, result.output
    assert "``" not in result.stdout
