"""Tests for sdf_lark_parser.py -- error handling and public API."""

from unittest.mock import patch

import pytest
from conftest import DATA_DIR
from lark import LarkError

from sdf_toolkit.parser.parser import (
    SDFLarkParser,
    _local,
    get_parser,
    parse_sdf,
    parse_sdf_file,
)


class TestSDFLarkParserErrors:
    def test_malformed_sdf_raises_lark_error(self):
        parser = SDFLarkParser()
        with pytest.raises(LarkError, match="SDF parsing failed"):
            parser.parse("THIS IS NOT VALID SDF")

    def test_generic_exception_path(self):
        parser = SDFLarkParser()
        with (
            patch.object(parser.parser, "parse", side_effect=RuntimeError("boom")),
            pytest.raises(RuntimeError, match="Unexpected error during SDF parsing"),
        ):
            parser.parse("(DELAYFILE)")

    def test_grammar_file_missing(self):
        with (
            patch("pathlib.Path.open", side_effect=FileNotFoundError("not found")),
            pytest.raises(FileNotFoundError, match="Grammar file not found"),
        ):
            SDFLarkParser()


class TestParseFile:
    def test_parse_file_success(self):
        parser = SDFLarkParser()
        result = parser.parse_file(DATA_DIR / "test1.sdf")
        assert result is not None
        assert hasattr(result, "cells")

    def test_parse_with_comments(self):
        """SDF files with // comments should parse successfully."""
        sdf_with_comments = """(DELAYFILE
            // This is a comment
            (SDFVERSION "3.0")
            (TIMESCALE 1ps)
            (CELL
                (CELLTYPE "BUF")
                (INSTANCE buf1)
                // Comment inside cell
                (DELAY
                    (ABSOLUTE
                        (IOPATH A Z (1.0:2.0:3.0)(4.0:5.0:6.0))
                    )
                )
            )
        )"""
        result = SDFLarkParser().parse(sdf_with_comments)
        assert len(result.cells) == 1
        assert "BUF" in result.cells

    @pytest.mark.parametrize(
        "comment_layout",
        [
            pytest.param(
                "/* banner */\n{sdf}",
                id="banner-before-delayfile",
            ),
            pytest.param(
                "/* banner\n   over two lines */\n{sdf}",
                id="multi-line",
            ),
            pytest.param(
                "{sdf}\n/* trailer */",
                id="after-delayfile",
            ),
        ],
    )
    def test_parse_with_block_comments(self, comment_layout: str):
        """C-style ``/* */`` comments are ignored, including before the first token."""
        sdf = """(DELAYFILE
            (SDFVERSION "3.0")
            (TIMESCALE 1ps)
            (CELL
                (CELLTYPE "BUF")
                (INSTANCE buf1)
                (DELAY (ABSOLUTE (IOPATH A Z (1.0:2.0:3.0))))
            )
        )"""
        result = SDFLarkParser().parse(comment_layout.format(sdf=sdf))
        assert "BUF" in result.cells

    def test_block_comment_is_non_greedy(self):
        """Two comments on one line must not merge and swallow the text between them."""
        sdf = """(DELAYFILE
            (SDFVERSION "3.0")
            (TIMESCALE 1ps)
            (CELL
                (CELLTYPE "BUF") /* a */ (INSTANCE buf1) /* b */
                (DELAY (ABSOLUTE (IOPATH A Z (1.0:2.0:3.0))))
            )
        )"""
        result = SDFLarkParser().parse(sdf)
        assert "buf1" in result.cells["BUF"]

    def test_parse_file_nonexistent(self):
        parser = SDFLarkParser()
        with pytest.raises(Exception, match="Error reading SDF file"):
            parser.parse_file("/nonexistent/path/file.sdf")


class TestModuleLevelFunctions:
    def test_parse_sdf_file(self):
        result = parse_sdf_file(DATA_DIR / "test1.sdf")
        assert result is not None
        assert hasattr(result, "cells")

    def test_get_parser_caching(self):
        if hasattr(_local, "parser"):
            del _local.parser
        p1 = get_parser()
        p2 = get_parser()
        assert p1 is p2

    def test_parse_sdf(self):
        sdf_content = (DATA_DIR / "test1.sdf").read_text()
        result = parse_sdf(sdf_content)
        assert result is not None

    def test_parse_sdf_no_state_leak(self):
        """Parsing the same file twice with one parser must yield identical results.

        This guards against mutable transformer state leaking between calls.
        """
        parser = SDFLarkParser()
        sdf_content = (DATA_DIR / "test1.sdf").read_text()

        result1 = parser.parse(sdf_content)
        result2 = parser.parse(sdf_content)

        # Sanity check: parsed file should have cells
        assert len(result1.cells) > 0
        # Same header
        assert result1.header == result2.header
        # Same cells (count and content)
        assert len(result1.cells) == len(result2.cells)
        for c1, c2 in zip(result1.cells, result2.cells, strict=True):
            assert c1 == c2
