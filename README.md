# SDF Timing Toolkit

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

A comprehensive Python library and CLI toolkit for parsing, analyzing, and manipulating Standard Delay Format (SDF) Timing Annotation files. Built with a robust [Lark](https://github.com/lark-parser/lark) parser, [NetworkX](https://networkx.org/) timing graphs, and a modern [Typer](https://typer.tiangolo.com/) CLI.

## Features

### Core Capabilities

- **SDF Parser & Writer**: Parse SDF files into Python data structures and emit them back as SDF
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
  - IOPATH delays, with RETAIN
  - INTERCONNECT, PORT, DEVICE and NETDELAY delays
  - PATHPULSE and PATHPULSEPERCENT pulse limits, and pulse limits inside a delay value
  - Timing checks (SETUP, HOLD, SETUPHOLD, RECOVERY, REMOVAL, RECREM, SKEW, BIDIRECTSKEW, WIDTH, PERIOD, NOCHANGE), with SCOND and CCOND
  - Timing environment (PATHCONSTRAINT, SUM, DIFF, PERIODCONSTRAINT, SKEWCONSTRAINT, ARRIVAL, DEPARTURE, SLACK, WAVEFORM)
  - LABEL entries
- **Standards**:
  - OVI SDF 3.0 and IEEE 1497 (P1497 Draft 0.10, SDF 4.0), chosen with `--standard [ovi-3.0|ieee-1497]` or `standard=`
  - IEEE 1497 rules by default, without an SDFVERSION check
  - OVI SDF 3.0 rejects LABEL, NETDELAY, BIDIRECTSKEW, `\"` in a QSTRING and the TIMESCALE units s, ms and fs
  - IEEE 1497 limits RETAIN to 1 to 3 delvals
  - A delay COND or CONDELSE wraps one IOPATH; `--loose-cond` (`loose_cond=True`) reads any number of delay entries
- **Specifications**:
  - Header metadata (design, vendor, timescale, date, temperature, voltage)
  - Edge specifications (posedge, negedge and the transitions 01, 10, 0z, z1, 1z, z0)
  - Conditional timing expressions, named COND and CONDELSE
  - Bus bits and bit ranges in port names
  - Delay rvalue lists of 1, 2, 3, 6 or 12 transitions (rise, fall, turn-off, ...); 4 or 5 read as 6 and 7 to 11 as 12
  - Triple values (min:typ:max), one per process corner, with signed values
- **Not supported**: INCLUDE raises `UnsupportedConstructError`, since the file it names is not read
- **Limitations**: the parser accepts some input the standards forbid, such as a negative HOLD limit, and `annotate` raises on SDF entries without an IEEE 1364 form. The Parsing and Export pages of the documentation list both

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

The `sdf-toolkit` command has 20 subcommands. `sdf-toolkit --version` prints
the installed version, and `sdf-toolkit COMMAND --help` lists the options of
one command. `--corner` takes `min`, `typ` or `max` (default `max`) and
`--edge` takes `rise`, `fall`, `worst` or `best` (default `worst`); both
ignore case. `--format` (`-f`) takes `json` (default) or `sdf`, and
`--output` (`-o`) writes to a file in place of stdout. Every command that
reads SDF takes `--standard [ovi-3.0|ieee-1497]`, which applies the rules of
that standard and checks the SDFVERSION, and `--loose-cond`, which reads a
delay COND around any number of entries. Without `--standard` the IEEE 1497
rules apply and the SDFVERSION is not checked.

### Exit codes and output streams

Exit codes follow diff(1).

| Code | Meaning |
|------|---------|
| 0 | Success: the files are identical, lint found no issue, verify passed |
| 1 | A finding or a data error: the files differ, lint reported an issue, negative slack, verify failed, no path between the pins, an `emit` JSON file that lacks a key, or any library error (`SDFToolkitError`), such as an SDF parse error, a PORT load without a unique driver, a merge conflict, SDF content annotation cannot express, or Yosys missing or failing |
| 2 | A usage error: a missing file or a directory, an unknown pin, a value outside an option's choices, `merge` with one file, or only one of `--highlight-source` and `--highlight-sink` |

stdout carries only the command result, so it can be piped. Status lines
(`Written to ...`, `No issues found.`, `Files are identical.`) go to stderr.
An error writes one `Error: <message>` line to stderr, with no traceback, and
exits 1.

### File I/O

| Command | Description | Options |
|---------|-------------|---------|
| `parse SDF_FILE` | Parse SDF to JSON or SDF | `--format` |
| `emit JSON_FILE` | Convert `parse --format json` output back to SDF | |
| `info SDF_FILE` | Display file summary with Rich tables | |

### Path Analysis

Pins are full names in the timing graph, such as `P1/z`.

| Command | Description | Options |
|---------|-------------|---------|
| `compose SDF_FILE SOURCE SINK` | Sum delays along paths from source to sink | `--verbose` (`-v`) |
| `verify SDF_FILE SOURCE SINK` | Verify path delay matches expected value; exits 1 on mismatch | `--expected` (required), `--tolerance` |
| `decompose` | Extract an unknown delay segment as total minus known | `--total`, `--known` (both required) |
| `critical-path SDF_FILE SOURCE SINK` | Find the slowest path between two pins | `--corner`, `--edge` |
| `rank-paths SDF_FILE SOURCE SINK` | Rank all paths by delay | `--corner`, `--edge`, `--descending/--ascending` (default slowest first), `--limit` (`-n`, 0 shows all) |
| `slack SDF_FILE SOURCE SINK PERIOD` | Compute slack (period - critical delay); exits 1 when negative | `--corner`, `--edge` |
| `batch-analysis SDF_FILE` | Analyze all endpoint pairs in bulk | `--corner`, `--edge`, `--limit` (`-n`, default 20, 0 shows all) |

### Analysis & Reporting

| Command | Description | Options |
|---------|-------------|---------|
| `stats SDF_FILE` | Aggregate delay statistics | `--corner`, `--edge` |
| `query SDF_FILE` | Filter entries by multiple criteria | `--cell-type`, `--instance`, `--entry-type` (each repeatable), `--pin-pattern`, `--min-delay`, `--max-delay`, `--corner`, `--edge`, `--format` |
| `lint SDF_FILE` | Validate SDF structure and semantics; exits 1 when it reports an issue | `--severity` (`error` or `warning`; omitted reports both) |
| `report SDF_FILE` | Generate a timing report | `--corner`, `--edge`, `--top-n`, `--period` |
| `diff FILE_A FILE_B` | Compare two SDF files with tolerance; exits 1 when they differ | `--tolerance`, `--normalize/--no-normalize`, `--target-timescale` |

### Transformations

| Command | Description | Options |
|---------|-------------|---------|
| `merge FILES...` | Combine two or more SDF files | `--strategy` (`error` (default), `keep-first` or `keep-last`), `--target-timescale`, `--format` |
| `normalize SDF_FILE` | Convert all delays to target timescale | `--target` (required), `--format` |
| `annotate SDF_FILE VERILOG_FILE` | Add specify blocks and net delays to a Verilog file that defines a module for every SDF `CELLTYPE` | `--output`, `--corner`, `--edge` |
| `annotate-transport SDF_FILE VERILOG_FILE --top TOP` | Rewire each sink of a flat netlist through a transport delay buffer, keeping per-instance and per-sink delays | `--library` (port-only cell views, repeatable), `--output`, `--corner`, `--edge`, `--standard`, `--loose-cond` |
| `port-to-interconnect SDF_FILE VERILOG_FILE` | Rewrite PORT delays as INTERCONNECT using a netlist | `--output`, `--top` (top module, default the SDF `DESIGN`) |

### Visualization

| Command | Description | Options |
|---------|-------------|---------|
| `dot SDF_FILE` | Export timing graph to Graphviz DOT format | `--output`, `--highlight-source` and `--highlight-sink` (together), `--cluster/--no-cluster` (group nodes by instance), `--corner`, `--edge` |

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

# Compute slack against clock period; exits 1 on a timing violation
sdf-toolkit slack design.sdf clk data_out 10.0 --corner max

# Rank all paths from fastest to slowest
sdf-toolkit rank-paths design.sdf clk data_out --ascending --limit 10

# Show the five slowest paths (--descending is the default)
sdf-toolkit rank-paths design.sdf clk data_out --descending -n 5

# Analyze all endpoint pairs in design
sdf-toolkit batch-analysis design.sdf --corner max --limit 20
```

### Querying and Filtering

```bash
# Query entries by cell type
sdf-toolkit query design.sdf --cell-type BUFX4 --cell-type INVX2

# Query entries of named instances
sdf-toolkit query design.sdf --instance top/u1 --instance top/u2

# Filter by delay threshold
sdf-toolkit query design.sdf --min-delay 1.0 --max-delay 5.0

# Match specific pins with regex
sdf-toolkit query design.sdf --pin-pattern "clk.*" --entry-type iopath

# Output filtered SDF
sdf-toolkit query design.sdf --cell-type BUFX4 --format sdf > buffers_only.sdf
```

### Validation and Linting

```bash
# Validate SDF structure; exits 1 when it reports an issue
sdf-toolkit lint design.sdf

# Show only errors
sdf-toolkit lint design.sdf --severity error

# Get statistics
sdf-toolkit stats design.sdf --corner max
```

### Comparison and Merging

```bash
# Compare two SDF files; exits 0 when identical, 1 when they differ
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

# Annotate Verilog with specify blocks and net delays. design.v must define a
# module for every CELLTYPE in design.sdf, the top-level DESIGN cell included.
sdf-toolkit annotate design.sdf design.v -o design_annotated.v

# Annotate a flat routed netlist with one transport delay buffer per sink pin.
sdf-toolkit annotate-transport design.sdf design.v --top top -l cells.v -o design_baked.v

# Rewrite PORT (sink-only) delays as INTERCONNECT (source -> sink) delays.
# The driver of each PORT load is recovered from the gate-level netlist via
# Yosys, so back-ends that emit PORT (e.g. Libero) become consumable by tools
# that only understand INTERCONNECT.
sdf-toolkit port-to-interconnect design.sdf netlist.v -o design_interconnect.sdf

# Pick the top module when the netlist name differs from the SDF DESIGN
sdf-toolkit port-to-interconnect design.sdf netlist.v --top chip_top

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

# Group the nodes of each instance into a Graphviz cluster
sdf-toolkit dot design.sdf --cluster -o timing.dot

# Render with Graphviz
dot -Tpng timing.dot -o timing.png
```

### Generate Reports

```bash
# Timing report
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
from decimal import Decimal

from sdf_toolkit.core.pathgraph import verify_path, decompose_delay, compute_slack
from sdf_toolkit.core.model import DelayValues, Values

# Verify a path matches expected delay: one (rise, fall) pair of triples
expected = DelayValues.of(Values(min=1.805, max=1.805), Values(min=1.795, max=1.795))
result = verify_path(
    graph, "P1/z", "P2/i", expected=expected, tolerance=Decimal("0.01")
)

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
slack = compute_slack(graph, "P1/z", "P2/i", period=Decimal(10), corner=Corner.MAX)
if slack is not None:
    if slack < 0:
        print(f"Timing violation: {abs(slack)}")
    else:
        print(f"Slack: {slack}")
```

### Delay Arithmetic

Every delay, check limit and constraint value is an exact
`decimal.Decimal`.  The parser reads each number from the text of the file,
so `0.1 + 0.2` along a path is exactly `0.3`.  Constructors also take an
`int` or a `float`; a float becomes the decimal its shortest `repr` writes,
so `Values(min=0.1)` holds `Decimal("0.1")`.  Writers print each value as
Python prints the float of the same value, so emitted SDF text is unchanged.

```python
from decimal import Decimal

from sdf_toolkit.core.model import DelayValues, Values

# Create delay values
a = Values(min=Decimal("1.0"), typ=Decimal("2.0"), max=Decimal("3.0"))
b = Values(min=0.5, typ=1, max=1.5)  # int and float convert exactly

# Arithmetic operations
c = a + b          # Addition
d = a - b          # Subtraction
e = -a             # Negation
f = a * 2          # Scaling by a Decimal or int
g = 2 * a          # Reverse scaling

print(c)  # Values(min=Decimal('1.5'), typ=Decimal('3.0'), max=Decimal('4.5'))

# Equality is exact; approx_eq compares within an absolute tolerance
assert Values(min=0.1) + Values(min=0.2) == Values(min=Decimal("0.3"))
assert a.approx_eq(a, tolerance=Decimal("1e-9"))
assert not a.approx_eq(b, tolerance=Decimal("1e-9"))

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
│   │   ├── identifiers.py   # SDF identifier escaping and path splitting
│   │   ├── model.py         # SDFFile, DelayValues, Values, entry types
│   │   ├── pathgraph.py     # Timing graph and path analysis
│   │   └── utils.py         # Helper functions
│   ├── io/                   # I/O operations
│   │   ├── annotate.py      # Verilog annotation
│   │   ├── sdfparse.py      # parse() and emit() entry points
│   │   ├── transport.py     # Per-sink transport delay annotation
│   │   └── writer.py        # SDF emission
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
  - [Typer](https://typer.tiangolo.com/) - CLI framework
  - [Rich](https://rich.readthedocs.io/) - Terminal formatting
  - [NetworkX](https://networkx.org/) - Graph library
  - [graphviz](https://graphviz.readthedocs.io/) - DOT export
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
