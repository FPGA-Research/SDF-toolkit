"""Programmatic builder for constructing SDFFile objects."""

from typing import TypeVar

from sdf_toolkit.core.model import (
    BaseEntry,
    CellsDict,
    DelayField,
    DelayPaths,
    Device,
    EdgeType,
    Hold,
    Interconnect,
    Iopath,
    PathConstraint,
    Period,
    Port,
    Recovery,
    Removal,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    TimingCheck,
    Values,
    Width,
)

_TC = TypeVar("_TC", bound=TimingCheck)
_BE = TypeVar("_BE", bound=BaseEntry)

# Delays can be passed as a pre-built DelayPaths or as a nested dict.
DelaysInput = DelayPaths | dict[str, dict[str, float | None]]


def _resolve_delays(delays: DelaysInput) -> DelayPaths:
    """Accept DelayPaths passthrough or convert from nested dict."""
    if isinstance(delays, DelayPaths):
        return delays
    return DelayPaths(
        **{name: Values(**delays[name]) for name in DelayField if name in delays}
    )


# ── Entry factory functions ─────────────────────────────────────────


def _make_two_pin_entry(
    cls: type[_BE],
    prefix: str,
    from_pin: str,
    to_pin: str,
    delays: DelaysInput,
    *,
    from_pin_edge: EdgeType | None = None,
    to_pin_edge: EdgeType | None = None,
    **extra: object,
) -> _BE:
    """Shared constructor for two-pin delay entries."""
    return cls(
        name=f"{prefix}_{from_pin}_{to_pin}",
        from_pin=from_pin,
        to_pin=to_pin,
        from_pin_edge=from_pin_edge,
        to_pin_edge=to_pin_edge,
        delay_paths=_resolve_delays(delays),
        **extra,
    )


def _make_single_pin_entry(
    cls: type[_BE],
    prefix: str,
    pin: str,
    delays: DelaysInput,
) -> _BE:
    """Shared constructor for single-pin delay entries (port, device)."""
    return cls(
        name=f"{prefix}_{pin}",
        from_pin=pin,
        to_pin=pin,
        delay_paths=_resolve_delays(delays),
    )


def make_iopath(
    from_pin: str,
    to_pin: str,
    delays: DelaysInput,
    *,
    from_pin_edge: EdgeType | None = None,
    to_pin_edge: EdgeType | None = None,
) -> Iopath:
    """Create an IOPATH delay entry."""
    return _make_two_pin_entry(
        Iopath,
        "iopath",
        from_pin,
        to_pin,
        delays,
        from_pin_edge=from_pin_edge,
        to_pin_edge=to_pin_edge,
    )


def make_interconnect(
    from_pin: str,
    to_pin: str,
    delays: DelaysInput,
    *,
    from_pin_edge: EdgeType | None = None,
    to_pin_edge: EdgeType | None = None,
) -> Interconnect:
    """Create an INTERCONNECT delay entry."""
    return _make_two_pin_entry(
        Interconnect,
        "interconnect",
        from_pin,
        to_pin,
        delays,
        from_pin_edge=from_pin_edge,
        to_pin_edge=to_pin_edge,
    )


def make_port(pin: str, delays: DelaysInput) -> Port:
    """Create a PORT delay entry."""
    return _make_single_pin_entry(Port, "port", pin, delays)


def make_device(pin: str, delays: DelaysInput) -> Device:
    """Create a DEVICE delay entry."""
    return _make_single_pin_entry(Device, "device", pin, delays)


def make_timing_check(
    cls: type[_TC],
    from_pin: str,
    to_pin: str,
    delays: DelaysInput,
    *,
    from_pin_edge: EdgeType | None = None,
    to_pin_edge: EdgeType | None = None,
    is_cond: bool = False,
    cond_equation: str | None = None,
) -> _TC:
    """Create a timing check entry of the given type."""
    return cls(
        name=f"{cls.__name__.lower()}_{from_pin}_{to_pin}",
        is_timing_check=True,
        is_cond=is_cond,
        cond_equation=cond_equation,
        from_pin=from_pin,
        to_pin=to_pin,
        from_pin_edge=from_pin_edge,
        to_pin_edge=to_pin_edge,
        delay_paths=_resolve_delays(delays),
    )


def make_path_constraint(
    from_pin: str,
    to_pin: str,
    delays: DelaysInput,
    *,
    from_pin_edge: EdgeType | None = None,
    to_pin_edge: EdgeType | None = None,
) -> PathConstraint:
    """Create a path constraint entry."""
    return _make_two_pin_entry(
        PathConstraint,
        "pathconstraint",
        from_pin,
        to_pin,
        delays,
        from_pin_edge=from_pin_edge,
        to_pin_edge=to_pin_edge,
        is_timing_env=True,
    )


class SDFBuilder:
    """Fluent builder for constructing SDFFile objects from scratch.

    Examples
    --------
    >>> from sdf_toolkit.core.builder import SDFBuilder
    >>> sdf = (
    ...     SDFBuilder()
    ...     .set_header(sdfversion="3.0", design="top")
    ...     .add_cell("BUF", "buf0")
    ...         .add_iopath("A", "Y", {"nominal": {"min": 1.0, "avg": 2.0, "max": 3.0}})
    ...     .build()
    ... )
    >>> sdf.header.sdfversion
    '3.0'
    >>> "BUF" in sdf.cells
    True
    """

    def __init__(self) -> None:
        self._header_kwargs: dict[str, str] = {}
        self._cells: CellsDict = {}

    def set_header(self, **kwargs: str) -> "SDFBuilder":
        """Set header fields on the SDF file.

        Parameters
        ----------
        **kwargs : str
            Keyword arguments corresponding to SDFHeader fields
            (e.g. sdfversion, design, vendor, timescale).

        Returns
        -------
        SDFBuilder
            This builder instance for method chaining.
        """
        self._header_kwargs.update(kwargs)
        return self

    def add_cell(self, cell_type: str, instance: str) -> "CellBuilder":
        """Start building a cell and return a CellBuilder.

        Parameters
        ----------
        cell_type : str
            The cell type name (e.g. "BUF", "FDRE").
        instance : str
            The cell instance name (e.g. "buf0", "reg0").

        Returns
        -------
        CellBuilder
            A new CellBuilder bound to this SDFBuilder.
        """
        cell_instances = self._cells.setdefault(cell_type, {})
        entries: dict[str, BaseEntry] = cell_instances.setdefault(instance, {})
        return CellBuilder(self, entries)

    def build(self) -> SDFFile:
        """Build and return the completed SDFFile.

        Returns
        -------
        SDFFile
            The fully constructed SDF file object.
        """
        return SDFFile(
            header=SDFHeader(**self._header_kwargs),
            cells=self._cells,
        )


class CellBuilder:
    """Builder for a single cell's timing entries.

    Entries are written directly into the parent SDFBuilder's cell storage.
    Use ``add_cell()`` or ``build()`` to move on after adding entries.
    """

    def __init__(
        self,
        parent: SDFBuilder,
        entries: dict[str, BaseEntry],
    ) -> None:
        self._parent = parent
        self._entries = entries

    def add_entry(self, entry: BaseEntry) -> "CellBuilder":
        """Store a pre-built entry and return self for chaining.

        On name collision, appends _1, _2, etc. to ensure unique keys.

        Parameters
        ----------
        entry : BaseEntry
            The entry to store.

        Returns
        -------
        CellBuilder
            This builder instance for method chaining.
        """
        base_name = entry.name
        key = base_name
        if key in self._entries:
            counter = 1
            while f"{base_name}_{counter}" in self._entries:
                counter += 1
            key = f"{base_name}_{counter}"
            entry.name = key
        self._entries[key] = entry
        return self

    def add_cell(self, cell_type: str, instance: str) -> "CellBuilder":
        """Start a new cell, delegating to the parent SDFBuilder.

        Parameters
        ----------
        cell_type : str
            The cell type name.
        instance : str
            The cell instance name.

        Returns
        -------
        CellBuilder
            A new CellBuilder for the new cell.
        """
        return self._parent.add_cell(cell_type, instance)

    def set_header(self, **kwargs: str) -> "SDFBuilder":
        """Set header fields, delegating to the parent SDFBuilder."""
        return self._parent.set_header(**kwargs)

    def build(self) -> SDFFile:
        """Build the SDFFile, delegating to the parent SDFBuilder."""
        return self._parent.build()

    def add_iopath(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
    ) -> "CellBuilder":
        """Add an IOPATH delay entry.

        Parameters
        ----------
        from_pin : str
            The input pin name.
        to_pin : str
            The output pin name.
        delays : DelaysInput
            Delay values as DelayPaths or a dict keyed by field name.
        from_pin_edge : EdgeType | None
            Optional edge type for the input pin.
        to_pin_edge : EdgeType | None
            Optional edge type for the output pin.

        Returns
        -------
        CellBuilder
            This builder instance for method chaining.

        Examples
        --------
        >>> from sdf_toolkit.core.builder import SDFBuilder
        >>> sdf = (
        ...     SDFBuilder()
        ...     .add_cell("INV", "i0")
        ...         .add_iopath("A", "Y", {
        ...             "nominal": {"min": 0.5, "avg": 1.0, "max": 1.5},
        ...         })
        ...     .build()
        ... )
        >>> entry = sdf.cells["INV"]["i0"]["iopath_A_Y"]
        >>> entry.delay_paths.nominal.max
        1.5
        """
        return self.add_entry(
            make_iopath(
                from_pin,
                to_pin,
                delays,
                from_pin_edge=from_pin_edge,
                to_pin_edge=to_pin_edge,
            )
        )

    def add_interconnect(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
    ) -> "CellBuilder":
        """Add an interconnect delay entry.

        Parameters
        ----------
        from_pin : str
            The source pin name.
        to_pin : str
            The destination pin name.
        delays : DelaysInput
            Delay values as DelayPaths or a dict keyed by field name.
        from_pin_edge : EdgeType | None
            Optional edge type for the source pin.
        to_pin_edge : EdgeType | None
            Optional edge type for the destination pin.

        Returns
        -------
        CellBuilder
            This builder instance for method chaining.
        """
        return self.add_entry(
            make_interconnect(
                from_pin,
                to_pin,
                delays,
                from_pin_edge=from_pin_edge,
                to_pin_edge=to_pin_edge,
            )
        )

    def add_port(
        self,
        pin: str,
        delays: DelaysInput,
    ) -> "CellBuilder":
        """Add a port delay entry.

        Parameters
        ----------
        pin : str
            The port pin name.
        delays : DelaysInput
            Delay values as DelayPaths or a dict keyed by field name.

        Returns
        -------
        CellBuilder
            This builder instance for method chaining.
        """
        return self.add_entry(make_port(pin, delays))

    def add_device(
        self,
        pin: str,
        delays: DelaysInput,
    ) -> "CellBuilder":
        """Add a device delay entry.

        Parameters
        ----------
        pin : str
            The device pin name.
        delays : DelaysInput
            Delay values as DelayPaths or a dict keyed by field name.

        Returns
        -------
        CellBuilder
            This builder instance for method chaining.
        """
        return self.add_entry(make_device(pin, delays))

    def _add_timing_check(
        self,
        cls: type[_TC],
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a timing check entry of the given type.

        Parameters
        ----------
        cls : type[TimingCheck]
            The timing check dataclass (Setup, Hold, etc.).
        from_pin : str
            The data/signal pin name.
        to_pin : str
            The clock/reference pin name.
        delays : DelaysInput
            Delay values as DelayPaths or a dict keyed by field name.
        from_pin_edge : EdgeType | None
            Optional edge type for the from pin.
        to_pin_edge : EdgeType | None
            Optional edge type for the to pin.
        is_cond : bool
            Whether this is a conditional timing check.
        cond_equation : str | None
            The condition equation string.

        Returns
        -------
        CellBuilder
            This builder instance for method chaining.
        """
        return self.add_entry(
            make_timing_check(
                cls,
                from_pin,
                to_pin,
                delays,
                from_pin_edge=from_pin_edge,
                to_pin_edge=to_pin_edge,
                is_cond=is_cond,
                cond_equation=cond_equation,
            )
        )

    def add_setup(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a setup timing check entry."""
        return self._add_timing_check(
            Setup,
            from_pin,
            to_pin,
            delays,
            from_pin_edge=from_pin_edge,
            to_pin_edge=to_pin_edge,
            is_cond=is_cond,
            cond_equation=cond_equation,
        )

    def add_hold(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a hold timing check entry."""
        return self._add_timing_check(
            Hold,
            from_pin,
            to_pin,
            delays,
            from_pin_edge=from_pin_edge,
            to_pin_edge=to_pin_edge,
            is_cond=is_cond,
            cond_equation=cond_equation,
        )

    def add_removal(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a removal timing check entry."""
        return self._add_timing_check(
            Removal,
            from_pin,
            to_pin,
            delays,
            from_pin_edge=from_pin_edge,
            to_pin_edge=to_pin_edge,
            is_cond=is_cond,
            cond_equation=cond_equation,
        )

    def add_recovery(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a recovery timing check entry."""
        return self._add_timing_check(
            Recovery,
            from_pin,
            to_pin,
            delays,
            from_pin_edge=from_pin_edge,
            to_pin_edge=to_pin_edge,
            is_cond=is_cond,
            cond_equation=cond_equation,
        )

    def add_setuphold(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a setuphold combined timing check entry."""
        return self._add_timing_check(
            SetupHold,
            from_pin,
            to_pin,
            delays,
            from_pin_edge=from_pin_edge,
            to_pin_edge=to_pin_edge,
            is_cond=is_cond,
            cond_equation=cond_equation,
        )

    def add_width(
        self,
        pin: str,
        delays: DelaysInput,
        *,
        pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a width timing check entry."""
        return self._add_timing_check(
            Width,
            pin,
            pin,
            delays,
            from_pin_edge=pin_edge,
            to_pin_edge=pin_edge,
            is_cond=is_cond,
            cond_equation=cond_equation,
        )

    def add_period(
        self,
        pin: str,
        delays: DelaysInput,
        *,
        pin_edge: EdgeType | None = None,
        is_cond: bool = False,
        cond_equation: str | None = None,
    ) -> "CellBuilder":
        """Add a period timing check entry."""
        return self._add_timing_check(
            Period,
            pin,
            pin,
            delays,
            from_pin_edge=pin_edge,
            to_pin_edge=pin_edge,
            is_cond=is_cond,
            cond_equation=cond_equation,
        )

    def add_path_constraint(
        self,
        from_pin: str,
        to_pin: str,
        delays: DelaysInput,
        *,
        from_pin_edge: EdgeType | None = None,
        to_pin_edge: EdgeType | None = None,
    ) -> "CellBuilder":
        """Add a path constraint entry.

        Parameters
        ----------
        from_pin : str
            The source pin name.
        to_pin : str
            The destination pin name.
        delays : DelaysInput
            Delay values as DelayPaths or a dict keyed by field name.
        from_pin_edge : EdgeType | None
            Optional edge type for the source pin.
        to_pin_edge : EdgeType | None
            Optional edge type for the destination pin.

        Returns
        -------
        CellBuilder
            This builder instance for method chaining.
        """
        return self.add_entry(
            make_path_constraint(
                from_pin,
                to_pin,
                delays,
                from_pin_edge=from_pin_edge,
                to_pin_edge=to_pin_edge,
            )
        )
