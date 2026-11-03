"""Fluent builder for constructing SDFFile objects.

The builder adds no meaning of its own: each ``add_*`` method constructs the
model entry of the same name and appends it to the current cell instance, so
entries keep the order they were added in, as a parsed file keeps file order.
Delay entries default to an ``ABSOLUTE`` block, the form back-annotation
tools write.
"""

from sdf_toolkit.core.model import (
    CellsDict,
    Cond,
    CondElse,
    DelayMode,
    DelayValues,
    Device,
    Divider,
    Entry,
    Hold,
    Interconnect,
    Iopath,
    PathConstraint,
    Period,
    Port,
    PortSpec,
    Recovery,
    Removal,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    Timescale,
    TimingPortSpec,
    Values,
    Width,
)


class SDFBuilder:
    """Fluent builder for constructing SDFFile objects from scratch.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> from sdf_toolkit.core.model import DelayValues, PortSpec, Values
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(sdfversion="3.0", design="top")
    ...     .add_cell("BUF", "buf0")
    ...     .add_iopath(
    ...         input=PortSpec("A"),
    ...         output=PortSpec("Y"),
    ...         delay=DelayValues.of(Values(min=1.0, typ=2.0, max=3.0)),
    ...     )
    ...     .build()
    ... )
    >>> sdf.header.sdfversion
    '3.0'
    >>> sdf.cells["BUF"]["buf0"][0].output.port
    'Y'
    """

    def __init__(self) -> None:
        self._header = SDFHeader()
        self._cells: CellsDict = {}

    def set_header(
        self,
        *,
        sdfversion: str | None = None,
        design: str | None = None,
        vendor: str | None = None,
        program: str | None = None,
        version: str | None = None,
        divider: Divider | None = None,
        date: str | None = None,
        voltage: str | None = None,
        process: str | None = None,
        temperature: str | None = None,
        timescale: Timescale | None = None,
    ) -> "SDFBuilder":
        """Set the given header fields; a later call overrides an earlier one."""
        self._header = self._header.merged(
            SDFHeader(
                sdfversion=sdfversion,
                design=design,
                vendor=vendor,
                program=program,
                version=version,
                divider=divider,
                date=date,
                voltage=voltage,
                process=process,
                temperature=temperature,
                timescale=timescale,
            )
        )
        return self

    def add_cell(self, cell_type: str, instance: str) -> "CellBuilder":
        """Start or continue the entries of *instance* of *cell_type*."""
        entries = self._cells.setdefault(cell_type, {}).setdefault(instance, [])
        return CellBuilder(self, entries)

    def build(self) -> SDFFile:
        """Return the SDF file built so far."""
        return SDFFile(header=self._header, cells=self._cells)


class CellBuilder:
    """Builder for the entries of one cell instance.

    Entries are written directly into the parent SDFBuilder's cell storage.
    Use ``add_cell()`` or ``build()`` to move on after adding entries.
    """

    def __init__(self, parent: SDFBuilder, entries: list[Entry]) -> None:
        self._parent = parent
        self._entries = entries

    def add_entry(self, entry: Entry) -> "CellBuilder":
        """Append a pre-built entry."""
        self._entries.append(entry)
        return self

    def add_cell(self, cell_type: str, instance: str) -> "CellBuilder":
        """Start a new cell, delegating to the parent SDFBuilder."""
        return self._parent.add_cell(cell_type, instance)

    def build(self) -> SDFFile:
        """Build the SDFFile, delegating to the parent SDFBuilder."""
        return self._parent.build()

    def add_iopath(
        self,
        *,
        input: PortSpec,  # noqa: A002
        output: PortSpec,
        delay: DelayValues,
        mode: DelayMode = DelayMode.ABSOLUTE,
        cond: Cond | CondElse | None = None,
    ) -> "CellBuilder":
        """Add an ``IOPATH`` delay."""
        return self.add_entry(
            Iopath(input=input, output=output, delay=delay, mode=mode, cond=cond)
        )

    def add_interconnect(
        self,
        *,
        source: PortSpec,
        load: PortSpec,
        delay: DelayValues,
        mode: DelayMode = DelayMode.ABSOLUTE,
        cond: Cond | CondElse | None = None,
    ) -> "CellBuilder":
        """Add an ``INTERCONNECT`` delay."""
        return self.add_entry(
            Interconnect(source=source, load=load, delay=delay, mode=mode, cond=cond)
        )

    def add_port(
        self,
        *,
        port: PortSpec,
        delay: DelayValues,
        mode: DelayMode = DelayMode.ABSOLUTE,
        cond: Cond | CondElse | None = None,
    ) -> "CellBuilder":
        """Add a ``PORT`` delay."""
        return self.add_entry(Port(port=port, delay=delay, mode=mode, cond=cond))

    def add_device(
        self,
        *,
        output: PortSpec | None,
        delay: DelayValues,
        mode: DelayMode = DelayMode.ABSOLUTE,
        cond: Cond | CondElse | None = None,
    ) -> "CellBuilder":
        """Add a ``DEVICE`` delay."""
        return self.add_entry(Device(output=output, delay=delay, mode=mode, cond=cond))

    def add_setup(
        self, *, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> "CellBuilder":
        """Add a ``SETUP`` check."""
        return self.add_entry(Setup(data=data, reference=reference, limit=limit))

    def add_hold(
        self, *, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> "CellBuilder":
        """Add a ``HOLD`` check."""
        return self.add_entry(Hold(data=data, reference=reference, limit=limit))

    def add_recovery(
        self, *, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> "CellBuilder":
        """Add a ``RECOVERY`` check."""
        return self.add_entry(Recovery(data=data, reference=reference, limit=limit))

    def add_removal(
        self, *, data: TimingPortSpec, reference: TimingPortSpec, limit: Values
    ) -> "CellBuilder":
        """Add a ``REMOVAL`` check."""
        return self.add_entry(Removal(data=data, reference=reference, limit=limit))

    def add_setuphold(
        self,
        *,
        data: TimingPortSpec,
        reference: TimingPortSpec,
        setup: Values,
        hold: Values,
    ) -> "CellBuilder":
        """Add a ``SETUPHOLD`` check."""
        return self.add_entry(
            SetupHold(data=data, reference=reference, setup=setup, hold=hold)
        )

    def add_width(self, *, port: TimingPortSpec, limit: Values) -> "CellBuilder":
        """Add a ``WIDTH`` check."""
        return self.add_entry(Width(port=port, limit=limit))

    def add_period(self, *, port: TimingPortSpec, limit: Values) -> "CellBuilder":
        """Add a ``PERIOD`` check."""
        return self.add_entry(Period(port=port, limit=limit))

    def add_path_constraint(
        self, *, ports: tuple[str, ...], rise: Values, fall: Values
    ) -> "CellBuilder":
        """Add a ``PATHCONSTRAINT``."""
        return self.add_entry(PathConstraint(ports=ports, rise=rise, fall=fall))
