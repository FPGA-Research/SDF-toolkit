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
`IOPATH`, `PORT`, `INTERCONNECT`, `DEVICE` or `NETDELAY` entries, a form some
tools write.  The option is independent of the standard.

```{eval-rst}
.. automodule:: sdf_toolkit.parser.standard
   :members:
   :undoc-members:
   :show-inheritance:
```

## Low-level parser

```{eval-rst}
.. automodule:: sdf_toolkit.parser.parser
   :members:
   :undoc-members:
   :show-inheritance:
```
