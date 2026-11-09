# SDF Timing Toolkit

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](LICENSE)

A Python library and CLI to parse, analyse and transform Standard Delay Format
(SDF) timing annotation files. It uses a [Lark](https://github.com/lark-parser/lark)
parser, [NetworkX](https://networkx.org/) timing graphs and a
[Typer](https://typer.tiangolo.com/) CLI.

This is an independent rewrite inspired by
[f4pga-sdf-timing](https://github.com/chipsalliance/f4pga-sdf-timing). It is
not an official F4PGA or CHIPS Alliance project.

## Installation

```bash
uv pip install sdf-toolkit
```

The toolkit needs Python 3.12 or later. Its runtime dependencies are Lark,
Typer, Rich, NetworkX and the graphviz package. The `annotate`, `annotate-transport`
and `port-to-interconnect` commands and the `annotate_verilog` and
`annotate_transport` functions need [Yosys](https://yosyshq.net/yosys/) on
`PATH`.

## SDF support

The standards are OVI SDF 3.0 and IEEE 1497
([IEEE 1497-2001](https://standards.ieee.org/standard/1497-2001.html),
[Wikipedia](https://en.wikipedia.org/wiki/Standard_Delay_Format)).

- **Timing entries**:
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
  - Header entries SDFVERSION, DESIGN, DATE, VENDOR, PROGRAM, VERSION, DIVIDER, VOLTAGE, PROCESS, TEMPERATURE and TIMESCALE
  - Edge specifications (posedge, negedge and the transitions 01, 10, 0z, z1, 1z, z0)
  - Conditional timing expressions, named COND and CONDELSE
  - Bus bits and bit ranges in port names
  - Delay rvalue lists of 1, 2, 3, 6 or 12 transitions (rise, fall, turn-off, ...); 4 or 5 read as 6 and 7 to 11 as 12
  - Triple values (min:typ:max), one per process corner, with signed values
- **Not supported**: INCLUDE raises `UnsupportedConstructError`, since the file it names is not read
- **Limitations**: the parser accepts some input the standards forbid, such as a negative HOLD limit, and `annotate` raises on SDF entries without an IEEE 1364 form. `docs/api/core/parsing.md` and `docs/api/export/export.md` in the repository list both

## CLI reference

The `sdf-toolkit` command has 21 subcommands. `sdf-toolkit --version` prints
the installed version, and `sdf-toolkit COMMAND --help` lists the options of
one command. `--corner` takes `min`, `typ` or `max` (default `max`) and
`--edge` takes `rise`, `fall`, `worst` or `best` (default `worst`); both
ignore case. `--format` (`-f`) takes `json` (default) or `sdf`, and
`--output` (`-o`) writes to a file in place of stdout. Every command that
reads SDF takes `--standard [ovi-3.0|ieee-1497]`, which applies the rules of
that standard and rejects an SDFVERSION that names the other one, and
`--loose-cond`, which reads a delay COND around any number of entries.
Without `--standard` the IEEE 1497 rules apply and the SDFVERSION is not
checked.

### Exit codes and output streams

Exit codes follow diff(1).

| Code | Meaning |
|------|---------|
| 0 | Success: the files are identical, lint found no issue, verify passed |
| 1 | A finding or a data error: the files differ, lint reported an issue, negative slack, verify failed, no path between the pins, an `emit` JSON file that lacks a key, or any library error (`SDFToolkitError`), such as an SDF parse error, a PORT load without a unique driver, a merge conflict, SDF content annotation cannot express, or Yosys missing or failing |
| 2 | A usage error: a missing file or a directory, an unknown pin, a value outside an option's choices, `merge` with one file, or only one of `--highlight-source` and `--highlight-sink` |

stdout carries only the command result, so it can be piped. Status lines
(`Written to ...`, `No issues found.`, `Files are identical.`) go to stderr.
An error writes `Error: <message>` to stderr, with no traceback, and exits 1.

### File I/O

| Command | Description | Options |
|---------|-------------|---------|
| `parse SDF_FILE` | Parse SDF to JSON or SDF | `--format` |
| `emit JSON_FILE` | Convert `parse --format json` output back to SDF | |
| `info SDF_FILE` | Display file summary with Rich tables | |

### Path analysis

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

### Analysis and reporting

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
| `annotate-transport SDF_FILE VERILOG_FILE --top TOP` | Rewire each sink of a flat netlist through a transport delay buffer, keeping per-instance and per-sink delays | `--library` (`-l`, port-only cell views, repeatable), `--output`, `--corner`, `--edge` |
| `port-to-interconnect SDF_FILE VERILOG_FILE` | Rewrite PORT delays as INTERCONNECT using a netlist | `--output`, `--top` (top module, default the SDF `DESIGN`) |

### Visualization

| Command | Description | Options |
|---------|-------------|---------|
| `dot SDF_FILE` | Export timing graph to Graphviz DOT format | `--output`, `--highlight-source` and `--highlight-sink` (together), `--cluster/--no-cluster` (group nodes by instance), `--corner`, `--edge` |

### Examples

```bash
# Parse to JSON, or write back as SDF
sdf-toolkit parse design.sdf > design.json
sdf-toolkit parse design.sdf --format sdf > roundtrip.sdf
sdf-toolkit info design.sdf

# Compose delays along every path, with an edge-by-edge breakdown
sdf-toolkit compose design.sdf P1/z P2/i --verbose

# Verify an expected delay
sdf-toolkit verify design.sdf P1/z P2/i \
    --expected '[{"min": null, "typ": null, "max": 2.5}]' \
    --tolerance 0.01

# Decompose an unknown delay segment
sdf-toolkit decompose \
    --total '[{"min": null, "typ": null, "max": 5.0}]' \
    --known '[{"min": null, "typ": null, "max": 2.0}]'

# Critical (slowest) path for setup timing: max corner, worst transition
sdf-toolkit critical-path design.sdf clk data_out --corner max --edge worst

# Slack against a clock period
sdf-toolkit slack design.sdf clk data_out 10.0 --corner max

# The ten fastest paths (--descending is the default)
sdf-toolkit rank-paths design.sdf clk data_out --ascending -n 10

# Critical path of every startpoint and endpoint pair
sdf-toolkit batch-analysis design.sdf --limit 20

# Entries of one cell type with a delay of at least 1.0, written as SDF
sdf-toolkit query design.sdf --cell-type BUFX4 --min-delay 1.0 --format sdf > buffers.sdf

sdf-toolkit lint design.sdf --severity error
sdf-toolkit stats design.sdf --corner max
sdf-toolkit report design.sdf --period 10.0 --top-n 20

# Compare after normalising both files to 1ps
sdf-toolkit diff design_v1.sdf design_v2.sdf \
    --normalize --target-timescale 1ps --tolerance 0.01

sdf-toolkit merge file1.sdf file2.sdf file3.sdf \
    --strategy keep-last --target-timescale 1ps --format sdf > merged.sdf
sdf-toolkit normalize design.sdf --target 1ns --format sdf > design_ns.sdf

# Annotate Verilog with specify blocks and net delays. design.v must define a
# module for every CELLTYPE in design.sdf, the top-level DESIGN cell included.
sdf-toolkit annotate design.sdf design.v -o design_annotated.v

# Annotate a flat routed netlist with one transport delay buffer per sink pin.
sdf-toolkit annotate-transport design.sdf design.v --top top -l cells.v -o design_baked.v

# Rewrite PORT (sink-only) delays as INTERCONNECT (source -> sink) delays.
# The driver of each PORT load is recovered from the gate-level netlist via
# Yosys, so back-ends that emit PORT (e.g. Libero) become consumable by tools
# that only understand INTERCONNECT. --top picks the top module when the
# netlist name differs from the SDF DESIGN.
sdf-toolkit port-to-interconnect design.sdf netlist.v --top chip_top -o design_interconnect.sdf

# Highlight a critical path, group the nodes of each instance into a cluster
# and render with Graphviz
sdf-toolkit dot design.sdf -o timing.dot \
    --highlight-source clk --highlight-sink data_out --cluster
dot -Tpng timing.dot -o timing.png
```

## Python API

### Parsing and emission

```python
from sdf_toolkit import parse, emit
from sdf_toolkit.core.delays import Interconnect, Iopath

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

From 4 million characters up, `parse` splits the file at cell boundaries and
parses the chunks in worker processes. Pass `workers=1` to keep the parse in
the calling process, or a count to fix it. A daemonic process, which may not
have children, and a platform without the fork start method always parse
serially.

### Timing graph and path analysis

```python
from sdf_toolkit import Corner, EdgeSelect
from sdf_toolkit.core.pathgraph import TimingGraph
from sdf_toolkit.analysis.paths import critical_path, rank_paths

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

### Path verification and decomposition

```python
from decimal import Decimal

from sdf_toolkit.analysis.paths import verify_path, decompose_delay, compute_slack
from sdf_toolkit.core.values import DelayValues, Values

# Verify a path matches expected delay: one (rise, fall) pair of triples
expected = DelayValues.of(Values(min=1.805, max=1.805), Values(min=1.795, max=1.795))
result = verify_path(
    graph, "P1/z", "P2/i", expected=expected, tolerance=Decimal("0.01")
)

if result.passed:
    print("Delay verification passed")
else:
    print("Delay mismatch")
    for i, actual in enumerate(result.actual):
        print(f"  Path {i+1}: {actual.to_list()}")

# Decompose: unknown = total - known
total = DelayValues.of(Values(max=5.0))
known = DelayValues.of(Values(max=2.0))
unknown = decompose_delay(total, known)
print(f"Unknown segment: {unknown.scalar()}")  # max corner, worst transition

# Compute slack
slack = compute_slack(graph, "P1/z", "P2/i", period=Decimal(10), corner=Corner.MAX)
if slack is not None:
    if slack < 0:
        print(f"Timing violation: {abs(slack)}")
    else:
        print(f"Slack: {slack}")
```

### Delay arithmetic

Every delay, check limit and constraint value is an exact
`decimal.Decimal`.  The parser reads each number from the text of the file,
so `0.1 + 0.2` along a path is exactly `0.3`.  Constructors also take an
`int` or a `float`; a float becomes the decimal its shortest `repr` writes,
so `Values(min=0.1)` holds `Decimal("0.1")`.  Writers print each value as
Python prints the float of the same value, so emitted SDF text is unchanged.
Scaling takes a `Decimal` or an `int`, not a `float`.

```python
from decimal import Decimal

from sdf_toolkit.core.values import DelayValues, Values

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
summed = rise_fall + with_z            # 6 rvalues
halved = rise_fall * Decimal("0.5")    # Scale every rvalue

# None propagation
partial = Values(min=1.0, typ=None, max=3.0)
scaled = partial * 2  # Values(min=Decimal('2.0'), typ=None, max=Decimal('6.0'))
```

### Querying and filtering

```python
from sdf_toolkit.analysis.query import query
from sdf_toolkit.core.enums import Corner, EdgeSelect, EntryType

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
from sdf_toolkit.core.enums import Corner

# Get aggregate statistics
stats = compute_stats(sdf, corner=Corner.MAX)

print(f"Cell types: {stats.total_cells}")
print(f"Instances: {stats.total_instances}")
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
    print(f"  {vd.field}: {vd.value_a} vs {vd.value_b} (delta {vd.delta})")
```

### Merging

```python
from sdf_toolkit.core.values import Timescale
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
from sdf_toolkit.core.values import Timescale
from sdf_toolkit.transform.normalize import normalize_delays

# Normalize all delays to nanoseconds; a file without TIMESCALE is read
# at the 1ns IEEE 1497 default
normalized = normalize_delays(sdf, Timescale.parse("1ns"))

# The header now says 1ns
output = emit(normalized)
```

### DOT export

```python
from sdf_toolkit import Corner, EdgeSelect
from sdf_toolkit.analysis.export import to_dot
from sdf_toolkit.analysis.paths import critical_path

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

### Batch analysis

```python
from sdf_toolkit.analysis.paths import batch_endpoint_analysis

# Analyze all startpoint-to-endpoint pairs
results = batch_endpoint_analysis(graph, corner=Corner.MAX)

# Results are sorted by critical delay (descending)
for result in results[:10]:
    print(f"{result.source} -> {result.sink}")
    print(f"  Critical: {result.critical_delay}")
    print(f"  Paths: {result.path_count}")
```

### Register timing and structural queries

These queries characterise a block from its own timing, as an FPGA fabric
generator does for each cell.  Every result is deterministic: ties follow the
order of the entries in the file or the pin names, never `PYTHONHASHSEED`.

```python
from decimal import Decimal

from sdf_toolkit.core.enums import Corner, EntryType
from sdf_toolkit.core.pathgraph import TimingGraph
from sdf_toolkit.analysis.paths import critical_path, fastest_path

# Without traverse_registers no path crosses a register's setup arc.
graph = TimingGraph(sdf, traverse_registers=False)

# Only the pins inside one instance; forward and reverse searches stay inside.
cell = graph.within("top/lc0")

# Setup and hold at a pin that feeds each register, against a clock pin.  Of
# a register's checks, whatever their edges or conditions, the worst counts.
for register in cell.register_inputs():
    timing = cell.register_input_timing("top/lc0/lut/A", "top/lc0/clkbuf/A", register)
    if timing is not None:
        print(register.data, timing.setup, timing.hold)

# Combinational delay: no path through a register's clock-to-output arc.
delay = critical_path(cell.combinational(), "top/lc0/lut/A", "top/lc0/mux/X")

# Clock-to-output: the slowest path from the clock through a register clock pin.
clk_to_q = critical_path(graph, "clk", "top/lc0/mux/X", through="top/lc0/ff/CLK")

# Earliest arrival, the counterpart of critical_path.
earliest = fastest_path(graph, "clk", "top/lc0/ff/CLK", corner=Corner.MIN)

# Fewest-arc path to the nearest of several pins, forward or backward.
path = graph.nearest_path("top/lc0/mux/X", graph.primary_outputs())
driver = graph.nearest_path("top/lc0/lut/A", graph.primary_inputs(), reverse=True)

# The earliest pin that several inputs all reach, such as a mux output.
mux_out = graph.convergence_pin(["in0", "in1", "in2"])


# A path whose cost sums a weight you choose for each arc, here each arc's
# own worst transition with register setup arcs free.
def weight(arc):
    if arc.entry_type in (EntryType.SETUP, EntryType.SETUPHOLD):
        return Decimal(0)
    return arc.delay.scalar(corner=Corner.MAX)


found = graph.lightest_path("in0", "top/lc0/mux/X", weight)
```

## Project structure

```
sdf-toolkit/
├── src/sdf_toolkit/
│   ├── __init__.py           # Public API re-exported from every subpackage
│   ├── __main__.py           # python -m sdf_toolkit entry point
│   ├── cli.py                # Typer CLI commands
│   ├── errors.py             # SDFToolkitError, base of every input data error
│   ├── analysis/             # Analysis over a parsed file or its timing graph
│   │   ├── __init__.py      # Package exports
│   │   ├── diff.py          # SDF comparison
│   │   ├── export.py        # DOT export
│   │   ├── paths.py         # Critical, fastest, ranked and batch paths, slack
│   │   ├── query.py         # Entry filtering
│   │   ├── report.py        # Report generation
│   │   ├── stats.py         # Statistics computation
│   │   └── validate.py      # Linting and validation
│   ├── core/                 # Data model and timing graph
│   │   ├── __init__.py      # Package exports
│   │   ├── builder.py       # Fluent SDFFile builder
│   │   ├── checks.py        # TIMINGCHECK entries
│   │   ├── delays.py        # DELAY and pulse entries, PortSpec, Cond, ArcKey
│   │   ├── enums.py         # SDF keyword enums
│   │   ├── identifiers.py   # SDF identifier escaping and path splitting
│   │   ├── pathgraph.py     # Timing graph and its longest-path searches
│   │   ├── sdffile.py       # SDFFile, SDFHeader, LABEL, dictionary form
│   │   ├── timingenv.py     # TIMINGENV entries
│   │   ├── utils.py         # Timescale conversion helpers
│   │   └── values.py        # Decimal numbers, Timescale, Values, DelayValues
│   ├── io/                   # Emission and Verilog annotation
│   │   ├── __init__.py      # Package exports
│   │   ├── annotate.py      # Verilog annotation with specify blocks and net delays
│   │   ├── sdfparse.py      # parse() and emit() entry points
│   │   ├── specify.py       # SDF entries to IEEE 1364 specify items
│   │   ├── transport.py     # Per-sink transport delay annotation
│   │   ├── writer.py        # SDF emission
│   │   └── yosys.py         # Typed Yosys JSON netlist and the Yosys runner
│   ├── parser/               # Lark parser
│   │   ├── __init__.py      # Package exports
│   │   ├── chunking.py      # Split a file into CELL blocks for parallel parsing
│   │   ├── errors.py        # SDFParseError for text the grammar rejects
│   │   ├── grammar.py       # Grammar loading
│   │   ├── parser.py        # Serial and parallel parse drivers
│   │   ├── sdf.lark         # SDF grammar
│   │   ├── standard.py      # SDFStandard and the options of one parse
│   │   └── transformers.py  # Lark tree -> Python objects
│   └── transform/            # Transformations
│       ├── __init__.py      # Package exports
│       ├── interconnect.py  # PORT to INTERCONNECT conversion (uses Yosys)
│       ├── merge.py         # Multi-file merging
│       └── normalize.py     # Timescale normalization
├── tests/                    # pytest test suite
└── docs/                     # Sphinx documentation in MyST Markdown, not published
```

## Development

```bash
git clone https://github.com/KelvinChung2000/sdf-toolkit.git
cd sdf-toolkit
uv sync --extra docs

uv run pytest                                      # tests and doctests
uv run pytest --cov=sdf_toolkit --cov-report=html  # with coverage
uv run ruff check --fix src/ tests/
uv run ruff format src/ tests/
uv run make -C docs html                           # Sphinx HTML in docs/_build/html
```

The pre-commit hooks in `.pre-commit-config.yaml` run Ruff and ty.

## License

Licensed under the Apache License, Version 2.0. See [LICENSE](LICENSE) for details.
