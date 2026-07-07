import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from conftest import DATA_DIR
from typer.testing import CliRunner

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
            {
                "fast": {"min": 1.805, "avg": None, "max": 1.805},
                "slow": {"min": 1.795, "avg": None, "max": 1.795},
            }
        )
        result = runner.invoke(
            app,
            ["verify", SPEC_EXAMPLE1, "P1/z", "P2/i", "--expected", expected],
        )
        assert result.exit_code == 0
        assert "PASS" in result.output

    def test_verify_fail(self) -> None:
        expected = json.dumps(
            {
                "fast": {"min": 999.0, "avg": None, "max": 999.0},
                "slow": {"min": 999.0, "avg": None, "max": 999.0},
            }
        )
        result = runner.invoke(
            app,
            ["verify", SPEC_EXAMPLE1, "P1/z", "P2/i", "--expected", expected],
        )
        assert result.exit_code == 1
        assert "FAIL" in result.output


class TestDecompose:
    def test_decompose(self) -> None:
        total = json.dumps({"nominal": {"min": 3.0, "avg": None, "max": 3.0}})
        known = json.dumps({"nominal": {"min": 1.0, "avg": None, "max": 1.0}})
        result = runner.invoke(app, ["decompose", "--total", total, "--known", known])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["nominal"]["min"] == 2.0
        assert data["nominal"]["max"] == 2.0


class TestCriticalPathCmd:
    def test_critical_path(self) -> None:
        result = runner.invoke(app, ["critical-path", SPEC_EXAMPLE1, "P1/z", "P2/i"])
        assert result.exit_code == 0
        assert "Critical path scalar" in result.output
        assert "->" in result.output

    def test_critical_path_with_field(self) -> None:
        result = runner.invoke(
            app,
            ["critical-path", SPEC_EXAMPLE1, "P1/z", "P2/i", "--field", "fast"],
        )
        assert result.exit_code == 0
        assert "Critical path scalar" in result.output

    def test_critical_path_no_path(self) -> None:
        result = runner.invoke(app, ["critical-path", SPEC_EXAMPLE1, "P2/i", "P1/z"])
        assert result.exit_code == 1
        assert "No path found" in result.output


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
        assert "No paths found" in result.output


class TestSlackCmd:
    def test_slack_positive(self) -> None:
        result = runner.invoke(app, ["slack", SPEC_EXAMPLE1, "P1/z", "P2/i", "10.0"])
        assert result.exit_code == 0
        assert "Slack:" in result.output

    def test_slack_negative(self) -> None:
        result = runner.invoke(app, ["slack", SPEC_EXAMPLE1, "P1/z", "P2/i", "0.5"])
        assert result.exit_code == 0
        assert "TIMING VIOLATION" in result.output

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
        assert out.exists()
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

    def test_lint_with_issues(self) -> None:
        """Lint a file that has issues to cover the table output path."""
        result = runner.invoke(app, ["lint", EMPTY_SDF])
        assert result.exit_code == 0
        assert "Lint Issues" in result.output

    def test_lint_severity_filter(self) -> None:
        result = runner.invoke(app, ["lint", SPEC_EXAMPLE1, "--severity", "error"])
        assert result.exit_code == 0


class TestStatsCmd:
    def test_stats_default(self) -> None:
        result = runner.invoke(app, ["stats", SPEC_EXAMPLE1])
        assert result.exit_code == 0
        assert "SDF Statistics" in result.output

    def test_stats_custom_field_metric(self) -> None:
        result = runner.invoke(
            app, ["stats", TEST1, "--field", "slow", "--metric", "min"]
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
        assert "identical" in result.output.lower()

    def test_diff_different_files(self) -> None:
        result = runner.invoke(app, ["diff", TEST1, SPEC_EXAMPLE1])
        assert result.exit_code == 0
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
        assert result.exit_code == 0
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
        assert "No endpoint pairs found" in result.output


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
