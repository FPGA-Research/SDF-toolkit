# Parsing

## High-level interface

```{eval-rst}
.. automodule:: sdf_toolkit.io.sdfparse
   :members:
   :undoc-members:
   :show-inheritance:
```

## Standards

One grammar reads both OVI SDF 3.0 (May 1995) and IEEE 1497, which P1497
Draft 0.10 calls SDF 4.0, and the parser rejects what the chosen standard
does not define.  `parse_sdf(text, standard=...)` and the CLI option
`--standard [ovi-3.0|ieee-1497]` choose it.  Without a standard the IEEE 1497
rules apply and the SDFVERSION is not checked, so a file of either standard
that uses no construct the other lacks parses unchanged.  An explicit
standard also rejects an SDFVERSION that names the other one with
`StandardMismatchError`.  The draft reads the version from the first of
`"1.0"`, `"2.0"`, `"2.1"`, `"3.0"` and `"4.0"` inside the string, so
`"IEEE 1497 4.0"` names IEEE 1497 and versions before 3.0 are not checked.

The table lists where the two standards differ.  A construct a standard
does not define raises `StandardViolationError`, whose message names the
construct, the standard and the option that reads it.

| Construct | OVI SDF 3.0 | IEEE 1497 (default) |
| --- | --- | --- |
| `LABEL` | rejected | read |
| `NETDELAY` | rejected | read |
| `BIDIRECTSKEW` | rejected | read |
| `\"` inside a QSTRING | rejected | read as an escaped quote |
| `TIMESCALE` units `s`, `ms` and `fs` | rejected | read |
| `RETAIN` delvals | any count | 1 to 3 |
| SDFVERSION under `--standard` | must not name 4.0 | must not name 3.0 |

Both standards let a delay `COND` or `CONDELSE` wrap exactly one `IOPATH`,
and the parser applies that rule under either.  `loose_cond=True`, or
`--loose-cond` on the CLI, reads a `COND` or `CONDELSE` around any number of
`IOPATH`, `PORT`, `INTERCONNECT`, `DEVICE` or `NETDELAY` entries.  The
option is independent of the standard.

```{eval-rst}
.. automodule:: sdf_toolkit.parser.standard
   :members:
   :undoc-members:
   :show-inheritance:
```

## Limitations

The parser departs from OVI SDF 3.0 and P1497 Draft 0.10 in the cases the
table lists, under either standard unless a row names one.  Most rows
accept input the text forbids.  The negative timing-check limits are kept
on purpose, since timing tools emit them, and `tests/data/modulo6.sdf`
carries a negative HOLD.  The other rows are checks not yet written.

| Rule in the text | Source | Parser behaviour |
| --- | --- | --- |
| A SETUP, HOLD, RECOVERY or REMOVAL limit is not negative | OVI SDF 3.0 timing check entries | Accepts a negative limit |
| A DELAYFILE holds at least one CELL | `delay_file ::= ( DELAYFILE sdf_header cell { cell } )` in both | Accepts a file with no CELL |
| The delvals of consecutive RETAIN entries increase strictly | OVI SDF 3.0 p. 3-21 | Does not compare RETAIN entries |
| The earliest ARRIVAL and DEPARTURE times are less than the latest | OVI SDF 3.0 ARRIVAL and DEPARTURE | Does not compare the four values |
| Both BIDIRECTSKEW limits are positive | P1497 Draft 0.10 BIDIRECTSKEW | Accepts a zero limit |
| A file names its SDFVERSION | OVI SDF 3.0 header | Accepts a file without one under `--standard ovi-3.0` |
| A QSTRING gives a backslash no special meaning | OVI SDF 3.0 p. 4-2 | Reads `\"` as an escaped quote, so `"C:\dir\"` raises `StandardViolationError` under `ovi-3.0` |
| A condition may use `?:` and `{}` concatenation | `simple_expression` in both | Raises `SDFParseError` |
| WAVEFORM offsets increase monotonically | OVI SDF 3.0 WAVEFORM | Reads "monotonically" as strictly, so equal offsets raise `SDFModelError` |

## Low-level parser

```{eval-rst}
.. automodule:: sdf_toolkit.parser.parser
   :members:
   :undoc-members:
   :show-inheritance:
```
