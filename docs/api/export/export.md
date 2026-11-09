# Export

## Graph export

```{eval-rst}
.. automodule:: sdf_toolkit.analysis.export
   :members:
   :undoc-members:
   :show-inheritance:
```

## Transport annotation

`annotate_transport` suits a flat routed netlist, where `annotate_verilog`
would collapse every instance of a cell type onto one specify block and every
load of a net onto one delay.  It rewires each sink pin through a buffer whose
delay is the driver's slowest arc plus that sink's PORT delay.  The buffer is a
transport delay, so a pulse narrower than the delay survives, as it does on a
routed wire.  A gate, net or path delay is inertial by default and swallows
such a pulse, and Icarus Verilog refuses net delays, so the buffer uses a
non-blocking intra-assignment delay.

The stages are public for a caller that edits the Yosys JSON between them:
`yosys_running`, `routed_delays`, `insert_transport_buffers` and
`write_transport_verilog`.  `routed_delays` raises `AnnotationError` for an
INTERCONNECT, an INCREMENT and an SDF with no TIMESCALE.

## Verilog annotation

`annotate_verilog` writes IEEE 1364-2005 specify items.  These are module
paths and edge-sensitive paths, `if` and `ifnone` state-dependent paths, and
the `$setup`, `$hold`, `$setuphold`, `$recovery`, `$removal`, `$width` and
`$period` timing checks.  `$setup` takes the data event first, and `$hold`
and `$setuphold` take the reference event first.  `$recovery` and
`$removal` keep the SDF port order, in which OVI SDF 3.0 puts the
asynchronous control signal first and the clock second.

### Limitations

An SDF entry without a form in this writer raises `AnnotationError`, which
names the entry and the reason.

| SDF entry | Reason |
| --- | --- |
| INCREMENT delays | Verilog states absolute delays only |
| PORT delays | No specify form; `port-to-interconnect` rewrites them as INTERCONNECT first, or `annotate_transport` keeps them per sink |
| DEVICE delays | Not implemented in this writer |
| NETDELAY inside a cell | A net delay, not a specify item |
| RETAIN | No IEEE 1364 specify path form |
| PATHPULSE, PATHPULSEPERCENT and LABEL | Each would be a specparam, which this writer does not emit |
| RECREM, SKEW and NOCHANGE | The SDF to IEEE 1364 port order of `$recrem`, `$skew` and `$nochange` is not confirmed |
| BIDIRECTSKEW | No IEEE 1364 system task |
| SCOND and CCOND | The `$setuphold` timestamp and timecheck conditions are not emitted |
| TIMINGENV entries | No Verilog form |

The tools below compiled a cell that uses every emitted form.  Each run
checks that the tool reads the output, and no simulation confirms that a
timing check fires.  Commercial simulators are not tested.

| Tool | Command | Result |
| --- | --- | --- |
| Icarus Verilog 12.0 | `iverilog -gspecify -g2005` and `-g2012` | Reads every form |
| Verilator 5.032 | `verilator --lint-only` | Reads every form |
| Yosys 0.70 | `read_verilog` | Reads every form |
| Yosys 0.70 | `read_verilog -specify` | Rejects `ifnone` and the two-argument `$width` and `$period` |

```{eval-rst}
.. automodule:: sdf_toolkit.io.transport
   :members:
   :undoc-members:
   :show-inheritance:
```

```{eval-rst}
.. automodule:: sdf_toolkit.io.annotate
   :members:
   :undoc-members:
   :show-inheritance:
```

```{eval-rst}
.. automodule:: sdf_toolkit.io.specify
   :members:
   :undoc-members:
   :show-inheritance:
```

```{eval-rst}
.. automodule:: sdf_toolkit.io.yosys
   :members:
   :undoc-members:
   :show-inheritance:
```
