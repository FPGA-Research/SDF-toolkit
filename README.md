# SDF Timing Toolkit

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

A comprehensive Python library and CLI toolkit for parsing, analyzing, and manipulating Standard Delay Format (SDF) Timing Annotation files. Built with a robust [Lark](https://github.com/lark-parser/lark) parser, [NetworkX](https://networkx.org/) timing graphs, and a modern [Typer](https://typer.tiangolo.com/) CLI.

## Features

### Core Capabilities

- **SDF Parser & Writer**: Parse SDF files into Python data structures and emit them back using Jinja2 templates
- **Timing Graph Analysis**: NetworkX-based graph for path finding, composition, and critical path analysis
- **Delay Arithmetic**: Arithmetic on Values and DelayValues (add, subtract, negate, scale, approximate equality)
- **Path Analysis**: 
  - Compose delays along multi-hop timing paths
  - Verify composed delays against expected values
  - Decompose total delays into unknown segments
  - Find critical paths and compute slack
  - Rank paths by delay
  - Batch endpoint-to-endpoint analysis

### SDF Format Support

- **Timing Entries**:
  - IOPATH delays
  - INTERCONNECT delays
  - PORT delays
  - DEVICE delays
  - Timing checks (SETUP, HOLD, RECOVERY, REMOVAL, WIDTH, SETUPHOLD)
  - PATH constraints
- **Specifications**:
  - Header metadata (design, vendor, timescale, date, temperature, voltage)
  - Edge specifications (posedge/negedge)
  - Conditional timing expressions
  - Delay rvalue lists of 1, 2, 3, 6 or 12 transitions (rise, fall, turn-off, ...)
  - Triple values (min:typ:max), one per process corner

### Analysis & Utilities

- **Validation & Linting**: Detect structural and semantic issues in SDF files
- **Statistics**: Aggregate delay statistics with entry type breakdowns
- **Querying**: Filter entries by cell type, instance, pins, delay thresholds
- **Diffing**: Compare two SDF files with configurable tolerance
- **Merging**: Combine multiple SDF files with conflict resolution strategies
- **Normalization**: Convert all delays to a target timescale
- **Verilog Annotation**: Generate Verilog specify blocks from SDF timing data
- **DOT Export**: Visualize timing graphs with critical path highlighting

### Modern Development

- **Type-safe**: Python dataclasses with full type annotations
- **Rich CLI**: Beautiful terminal output with tables and colors
- **Comprehensive Testing**: pytest test suite with sample SDF files
- **Documentation**: Sphinx documentation with API reference

## About This Project

This is an independent modernized rewrite inspired by the original [f4pga-sdf-timing](https://github.com/chipsalliance/f4pga-sdf-timing) project. It represents a complete rebuild using modern Python practices (3.12+), a Lark-based parser for robustness, and an expanded feature set for comprehensive SDF timing analysis.

**Note:** This is not an official F4PGA or CHIPS Alliance project, but an independent effort to provide modern SDF utility.

## Installation

```bash
uv pip install sdf_toolkit
```

For development:

```bash
git clone https://github.com/KelvinChung2000/sdf-toolkit.git
cd sdf-toolkit
uv sync
```

## Quick Start

### Command Line

```bash
# Parse and inspect an SDF file
sdf-toolkit info design.sdf

# Find critical path between two pins
sdf-toolkit critical-path design.sdf clk_input data_output

# Generate timing report
sdf-toolkit report design.sdf --period 10.0

# Query specific entries
sdf-toolkit query design.sdf --cell-type BUFX4 --min-delay 1.0
```

### Python API

```python
from sdf_toolkit import parse, TimingGraph

# Parse SDF file
with open("design.sdf") as f:
    sdf = parse(f.read())

# Build timing graph and analyze
graph = TimingGraph(sdf)
delays = graph.compose("input_pin", "output_pin")
print(f"Path delay: {delays[0].scalar()}")  # max corner, worst transition
```

From 4 MB up, `parse` splits the file at cell boundaries and parses the
chunks in worker processes. Pass `workers=1` to keep the parse in the
calling process, or a count to fix it; a daemonic process, which may not
have children, always parses serially.

## CLI Reference

The `sdf-toolkit` command provides 20 subcommands for comprehensive SDF manipulation:

### File I/O

| Command | Description |
|---------|-------------|
| `parse` | Parse SDF to JSON or normalized SDF |
| `emit` | Convert JSON back to SDF format |
| `info` | Display file summary with Rich tables |

### Path Analysis

| Command | Description |
|---------|-------------|
| `compose` | Sum delays along paths from source to sink |
| `verify` | Verify path delay matches expected value |
| `decompose` | Extract unknown delay segments |
| `critical-path` | Find the slowest path between two pins |
| `rank-paths` | Rank all paths by delay (fastest/slowest) |
| `slack` | Compute slack (period - critical delay) |
| `batch-analysis` | Analyze all endpoint pairs in bulk |

### Analysis & Reporting

| Command | Description |
|---------|-------------|
| `stats` | Aggregate delay statistics |
| `query` | Filter entries by multiple criteria |
| `lint` | Validate SDF structure and semantics |
| `report` | Generate comprehensive timing report |
| `diff` | Compare two SDF files with tolerance |

### Transformations

| Command | Description |
|---------|-------------|
| `merge` | Combine multiple SDF files |
| `normalize` | Convert all delays to target timescale |
| `annotate` | Generate Verilog specify blocks |
| `port-to-interconnect` | Rewrite PORT delays as INTERCONNECT using a netlist |

### Visualization

| Command | Description |
|---------|-------------|
| `dot` | Export timing graph to Graphviz DOT format |

## Usage Examples

### Parse and Inspect

```bash
# Parse to JSON
sdf-toolkit parse design.sdf > design.json

# Parse and write back as SDF
sdf-toolkit parse design.sdf --format sdf > roundtrip.sdf

# Show summary information
sdf-toolkit info design.sdf
```

### Path Composition and Verification

```bash
# Find all paths and compose delays
sdf-toolkit compose design.sdf P1/z P2/i

# Show detailed edge-by-edge breakdown
sdf-toolkit compose design.sdf P1/z P2/i --verbose

# Verify expected delay (returns exit code 0 on match, 1 on mismatch)
sdf-toolkit verify design.sdf P1/z P2/i \
    --expected '[{"min": null, "typ": null, "max": 2.5}]' \
    --tolerance 0.01
```

### Critical Path Analysis

```bash
# Find critical (slowest) path for setup timing: max corner, worst transition
sdf-toolkit critical-path design.sdf clk data_out --corner max --edge worst

# Compute slack against clock period
sdf-toolkit slack design.sdf clk data_out 10.0 --corner max

# Rank all paths from fastest to slowest
sdf-toolkit rank-paths design.sdf clk data_out --ascending --limit 10

# Analyze all endpoint pairs in design
sdf-toolkit batch-analysis design.sdf --corner max --limit 20
```

### Querying and Filtering

```bash
# Query entries by cell type
sdf-toolkit query design.sdf --cell-type BUFX4 --cell-type INVX2

# Filter by delay threshold
sdf-toolkit query design.sdf --min-delay 1.0 --max-delay 5.0

# Match specific pins with regex
sdf-toolkit query design.sdf --pin-pattern "clk.*" --entry-type iopath

# Output filtered SDF
sdf-toolkit query design.sdf --cell-type BUFX4 --format sdf > buffers_only.sdf
```

### Validation and Linting

```bash
# Validate SDF structure
sdf-toolkit lint design.sdf

# Show only errors
sdf-toolkit lint design.sdf --severity error

# Get statistics
sdf-toolkit stats design.sdf --corner max
```

### Comparison and Merging

```bash
# Compare two SDF files
sdf-toolkit diff design_v1.sdf design_v2.sdf --tolerance 0.01

# Normalize before comparing
sdf-toolkit diff design_v1.sdf design_v2.sdf \
    --normalize --target-timescale 1ps

# Merge multiple files
sdf-toolkit merge file1.sdf file2.sdf file3.sdf \
    --strategy keep-last \
    --target-timescale 1ps \
    --format sdf > merged.sdf
```

### Transformations

```bash
# Normalize to nanoseconds
sdf-toolkit normalize design.sdf --target 1ns --format sdf > design_ns.sdf

# Annotate Verilog with specify blocks
sdf-toolkit annotate design.sdf cells.v -o annotated_cells.v

# Rewrite PORT (sink-only) delays as INTERCONNECT (source -> sink) delays.
# The driver of each PORT load is recovered from the gate-level netlist via
# Yosys, so back-ends that emit PORT (e.g. Libero) become consumable by tools
# that only understand INTERCONNECT.
sdf-toolkit port-to-interconnect design.sdf netlist.v -o design_interconnect.sdf

# Decompose unknown delay
sdf-toolkit decompose \
    --total '[{"min": null, "typ": null, "max": 5.0}]' \
    --known '[{"min": null, "typ": null, "max": 2.0}]'
```

### Visualization

```bash
# Export timing graph
sdf-toolkit dot design.sdf -o timing.dot

# Export with critical path highlighting
sdf-toolkit dot design.sdf -o timing.dot \
    --highlight-source clk \
    --highlight-sink data_out \
    --corner max --edge worst

# Render with Graphviz
dot -Tpng timing.dot -o timing.png
```

### Generate Reports

```bash
# Comprehensive timing report
sdf-toolkit report design.sdf --corner max

# With slack analysis
sdf-toolkit report design.sdf --period 10.0 --top-n 20
```

## Python API

### Basic Parsing and Emission

```python
from sdf_toolkit import parse, emit
from sdf_toolkit.core.model import Interconnect, Iopath

# Parse SDF text
with open("design.sdf") as f:
    sdf = parse(f.read())

# Access header
print(f"Design: {sdf.header.design}")
print(f"Timescale: {sdf.header.timescale}")
print(f"Temperature: {sdf.header.temperature}")

# Iterate through timing entries; each instance holds a list in file order
for cell_type, instances in sdf.cells.items():
    for instance, entries in instances.items():
        for entry in entries:
            print(f"{cell_type}/{instance}: {entry.key}")
            match entry:
                case Iopath() | Interconnect():
                    print(f"  Delay: {entry.delay.to_list()}")

# Emit back to SDF; the header's TIMESCALE is written as parsed
sdf_text = emit(sdf)
```

### Timing Graph and Path Analysis

```python
from sdf_toolkit import Corner, EdgeSelect
from sdf_toolkit.core.pathgraph import TimingGraph, critical_path, rank_paths

# Build timing graph
graph = TimingGraph(sdf)

# Explore graph structure
print(f"Nodes: {len(graph.nodes())}")
print(f"Edges: {len(graph.edges())}")

# Find all paths between two pins
paths = graph.find_paths("P1/z", "P2/i")
print(f"Found {len(paths)} paths")

# Compose delays along each path
for i, path in enumerate(paths):
    delay = graph.compose_delay(path)
    print(f"Path {i+1}: {delay.scalar(corner=Corner.MAX)}")
    
# Or compose all at once
all_delays = graph.compose("P1/z", "P2/i")

# Find critical path
cp = critical_path(graph, "P1/z", "P2/i", corner=Corner.MAX, edge=EdgeSelect.WORST)
if cp:
    print(f"Critical delay: {cp.scalar}")
    for timing_edge in cp.edges:
        print(f"  {timing_edge.source} -> {timing_edge.sink}")

# Rank all paths
ranked = rank_paths(graph, "P1/z", "P2/i", corner=Corner.MAX)
for i, rp in enumerate(ranked[:10]):
    print(f"#{i+1}: {rp.scalar}")
```

### Path Verification and Decomposition

```python
from sdf_toolkit.core.pathgraph import verify_path, decompose_delay, compute_slack
from sdf_toolkit.core.model import DelayValues, Values

# Verify a path matches expected delay: one (rise, fall) pair of triples
expected = DelayValues.of(Values(min=1.805, max=1.805), Values(min=1.795, max=1.795))
result = verify_path(graph, "P1/z", "P2/i", expected=expected, tolerance=0.01)

if result.passed:
    print("✓ Delay verification passed")
else:
    print("✗ Delay mismatch")
    for i, actual in enumerate(result.actual):
        print(f"  Path {i+1}: {actual.to_list()}")

# Decompose: unknown = total - known
total = DelayValues.of(Values(max=5.0))
known = DelayValues.of(Values(max=2.0))
unknown = decompose_delay(total, known)
print(f"Unknown segment: {unknown.scalar()}")

# Compute slack
slack = compute_slack(graph, "P1/z", "P2/i", period=10.0, corner=Corner.MAX)
if slack is not None:
    if slack < 0:
        print(f"Timing violation: {abs(slack)}")
    else:
        print(f"Slack: {slack}")
```

### Delay Arithmetic

```python
from sdf_toolkit.core.model import DelayValues, Values

# Create delay values
a = Values(min=1.0, typ=2.0, max=3.0)
b = Values(min=0.5, typ=1.0, max=1.5)

# Arithmetic operations
c = a + b          # Addition
d = a - b          # Subtraction
e = -a             # Negation
f = a * 2.0        # Scaling
g = 2.0 * a        # Reverse scaling

print(c)  # Values(min=1.5, typ=3.0, max=4.5)

# Approximate equality with tolerance
assert a.approx_eq(a, tolerance=1e-9)
assert not a.approx_eq(b, tolerance=1e-9)

# DelayValues arithmetic works transition by transition.  Rvalue lists of
# different lengths are aligned first, so (rise, fall) + (rise, fall, z)
# expands both to the six-transition form.
rise_fall = DelayValues.of(Values.uniform(1.0), Values.uniform(2.0))
with_z = DelayValues.of(Values.uniform(0.5), Values.uniform(0.5), Values.uniform(0.1))
summed = rise_fall + with_z  # 6 rvalues
halved = rise_fall * 0.5     # Scale every rvalue

# None propagation
partial = Values(min=1.0, typ=None, max=3.0)
scaled = partial * 2.0  # Values(min=2.0, typ=None, max=6.0)
```

### Querying and Filtering

```python
from sdf_toolkit.analysis.query import query
from sdf_toolkit.core.model import Corner, EdgeSelect, EntryType

# Query specific entries
filtered = query(
    sdf,
    cell_types=["BUFX4", "INVX2"],
    entry_types=[EntryType.IOPATH],
    min_delay=1.0,
    max_delay=5.0,
    corner=Corner.MAX,
    edge=EdgeSelect.WORST,
)

# filtered is a new SDFFile with matching entries
for cell_type, instances in filtered.cells.items():
    print(f"Cell type: {cell_type}")
```

### Validation

```python
from sdf_toolkit.analysis.validate import validate

# Validate SDF structure
issues = validate(sdf)

for issue in issues:
    print(f"[{issue.severity}] {issue.message}")
    if issue.cell_type:
        print(f"  Cell: {issue.cell_type}/{issue.instance}")
```

### Statistics

```python
from sdf_toolkit.analysis.stats import compute_stats
from sdf_toolkit.core.model import Corner

# Get aggregate statistics
stats = compute_stats(sdf, corner=Corner.MAX)

print(f"Total cells: {stats.total_instances}")
print(f"Total entries: {stats.total_entries}")
print(f"Delay range: {stats.delay_min} - {stats.delay_max}")
print(f"Mean delay: {stats.delay_mean}")
print(f"Median delay: {stats.delay_median}")

for entry_type, count in stats.entry_type_counts.items():
    print(f"  {entry_type}: {count}")
```

### Diffing

```python
from sdf_toolkit.analysis.diff import diff

# Compare two SDF files
sdf_a = parse(open("design_v1.sdf").read())
sdf_b = parse(open("design_v2.sdf").read())

result = diff(sdf_a, sdf_b, tolerance=0.01, normalize_first=True)

# Check header differences
for field, (val_a, val_b) in result.header_diffs.items():
    print(f"Header {field}: {val_a} vs {val_b}")

# Entries only in A
print(f"Only in A: {len(result.only_in_a)} entries")

# Entries only in B
print(f"Only in B: {len(result.only_in_b)} entries")

# Entries match by arc key, so reordered files give no differences.
# Value differences
for vd in result.value_diffs:
    print(f"{vd.cell_type}/{vd.instance}/{vd.key} #{vd.occurrence}")
    print(f"  {vd.field}: {vd.value_a} vs {vd.value_b} (Δ {vd.delta})")
```

### Merging

```python
from sdf_toolkit.core.model import Timescale
from sdf_toolkit.transform.merge import merge, ConflictStrategy

# Merge multiple SDF files
sdf_files = [parse(open(f).read()) for f in ["a.sdf", "b.sdf", "c.sdf"]]

merged = merge(
    sdf_files,
    strategy=ConflictStrategy.KEEP_LAST,
    target_timescale=Timescale.parse("1ps"),
)

# Write merged result; its header TIMESCALE is 1ps
output = emit(merged)
```

### Normalization

```python
from sdf_toolkit.core.model import Timescale
from sdf_toolkit.transform.normalize import normalize_delays

# Normalize all delays to nanoseconds; a file without TIMESCALE is read
# at the 1ns IEEE 1497 default
normalized = normalize_delays(sdf, Timescale.parse("1ns"))

# The header now says 1ns
output = emit(normalized)
```

### DOT Export

```python
from sdf_toolkit import Corner, EdgeSelect
from sdf_toolkit.analysis.export import to_dot
from sdf_toolkit.core.pathgraph import critical_path

# Export basic timing graph
dot_text = to_dot(graph)
with open("timing.dot", "w") as f:
    f.write(dot_text)

# Export with critical path highlighting
dot_text = to_dot(
    graph,
    highlight_path=critical_path(graph, "P1/z", "P2/i"),
    cluster_by_instance=True,
    corner=Corner.MAX,
    edge=EdgeSelect.WORST,
)
```

### Batch Analysis

```python
from sdf_toolkit.core.pathgraph import batch_endpoint_analysis

# Analyze all startpoint-to-endpoint pairs
results = batch_endpoint_analysis(graph, corner=Corner.MAX)

# Results are sorted by critical delay (descending)
for result in results[:10]:
    print(f"{result.source} → {result.sink}")
    print(f"  Critical: {result.critical_delay}")
    print(f"  Paths: {result.path_count}")
```

## Project Structure

```
sdf-toolkit/
├── src/sdf_toolkit/
│   ├── __init__.py           # Main exports (parse, emit, TimingGraph, etc.)
│   ├── __main__.py           # CLI entry point
│   ├── cli.py                # Typer CLI commands
│   ├── analysis/             # Analysis utilities
│   │   ├── diff.py          # SDF comparison
│   │   ├── export.py        # DOT export
│   │   ├── query.py         # Entry filtering
│   │   ├── report.py        # Report generation
│   │   ├── stats.py         # Statistics computation
│   │   └── validate.py      # Linting and validation
│   ├── core/                 # Core data structures
│   │   ├── builder.py       # SDFFile builder utilities
│   │   ├── model.py         # SDFFile, DelayValues, Values, entry types
│   │   ├── pathgraph.py     # Timing graph and path analysis
│   │   └── utils.py         # Helper functions
│   ├── io/                   # I/O operations
│   │   ├── annotate.py      # Verilog annotation
│   │   ├── sdfparse.py      # parse() and emit() entry points
│   │   ├── writer.py        # SDF emission (Jinja2)
│   │   └── templates/       # Jinja2 templates for SDF output
│   ├── parser/               # Lark parser
│   │   ├── chunking.py      # Split a file into CELL blocks for parallel parsing
│   │   ├── grammar.py       # Grammar loading
│   │   ├── parser.py        # Serial and parallel parse drivers
│   │   ├── sdf.lark         # SDF grammar
│   │   └── transformers.py  # Lark tree -> Python objects
│   └── transform/            # Transformations
│       ├── interconnect.py  # PORT to INTERCONNECT conversion (uses Yosys)
│       ├── merge.py         # Multi-file merging
│       └── normalize.py     # Timescale normalization
├── tests/                    # pytest test suite
└── docs/                     # Sphinx documentation
```

## Requirements

- **Python**: 3.12 or higher
- **Dependencies**:
  - [Lark](https://github.com/lark-parser/lark) - Parser generator
  - [Jinja2](https://jinja.palletsprojects.com/) - Templating engine
  - [Typer](https://typer.tiangolo.com/) - CLI framework
  - [Rich](https://rich.readthedocs.io/) - Terminal formatting
  - [NetworkX](https://networkx.org/) - Graph library
- **Optional**: [Yosys](https://yosyshq.net/yosys/) on `PATH` for Verilog annotation and PORT-to-INTERCONNECT conversion

## Development

### Setup

```bash
git clone https://github.com/KelvinChung2000/sdf-toolkit.git
cd sdf-toolkit
uv sync --extra docs
```

### Testing

```bash
# Run all tests
uv run pytest

# Run with coverage
uv run pytest --cov=sdf_toolkit --cov-report=html

# Run specific test file
uv run pytest tests/test_parser.py

# Run with verbose output
uv run pytest -v
```

### Code Quality

```bash
# Check formatting and linting
uv run ruff check src/ tests/

# Auto-fix issues
uv run ruff check --fix src/ tests/

# Format code
uv run ruff format src/ tests/
```

### Documentation

```bash
# Build HTML documentation
cd docs
make html

# View in browser
open _build/html/index.html
```

### Project Management

This project uses:
- **UV**: Fast Python package installer and resolver
- **pytest**: Testing framework with coverage
- **Ruff**: Fast Python linter and formatter
- **Sphinx**: Documentation generator with MyST markdown support
- **pre-commit**: Git hooks for code quality

## About Standard Delay Format

From Wikipedia:

> Standard Delay Format (SDF) is an IEEE standard for the representation and
> interpretation of timing data for use at any stage of an electronic design
> process. It finds wide applicability in design flows, and forms an efficient
> bridge between
> [Dynamic timing verification](https://en.wikipedia.org/wiki/Dynamic_timing_verification) and
> [Static timing analysis](https://en.wikipedia.org/wiki/Static_timing_analysis).
>
> It is an ASCII format that is represented in a tool and language independent
> way and includes path delays, timing constraint values, interconnect delays
> and high level technology parameters.
>
> SDF format can be used for back-annotation as well as forward-annotation.

SDF is widely used in ASIC and FPGA design flows for:
- Post-synthesis timing verification
- Post-layout timing back-annotation
- Gate-level simulation with timing
- Static timing analysis (STA)
- Timing-driven placement and routing

## Resources

### Documentation & Standards

- [IEEE 1497-2001 Standard](https://standards.ieee.org/standard/1497-2001.html) - Official SDF specification
- [Standard Delay Format (Wikipedia)](https://en.wikipedia.org/wiki/Standard_Delay_Format)
- [Project Documentation](https://sdf-toolkit.readthedocs.io/)

### Related Tools

- [Lark Parser](https://github.com/lark-parser/lark) - The parser generator used by this project
- [Verilog To Routing](https://docs.verilogtorouting.org/) - Open-source FPGA CAD flow with SDF generation
- [Yosys](https://yosyshq.net/yosys/) - Open-source synthesis suite
- [Icarus Verilog](http://iverilog.icarus.com/) - Verilog simulator supporting SDF annotation

### Community

- [GitHub Repository](https://github.com/KelvinChung2000/sdf-toolkit)
- [Original F4PGA SDF Timing](https://github.com/chipsalliance/f4pga-sdf-timing) - Original project this was inspired by

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.

## Contributing

Contributions are welcome! Please:

1. **Fork** the repository
2. **Create** a feature branch (`git checkout -b feature/amazing-feature`)
3. **Commit** your changes (`git commit -m 'Add amazing feature'`)
4. **Push** to the branch (`git push origin feature/amazing-feature`)
5. **Open** a pull request

### Guidelines

- Follow PEP 8 style guide (enforced by Ruff)
- Add tests for new features
- Update documentation as needed
- Ensure all tests pass before submitting PR
- Write clear commit messages

## Acknowledgments

This project was inspired by the original [f4pga-sdf-timing](https://github.com/chipsalliance/f4pga-sdf-timing) project from the CHIPS Alliance. While this is an independent rewrite, it acknowledges the pioneering work done by the F4PGA community in creating open-source FPGA timing tools.
