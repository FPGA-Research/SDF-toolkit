"""SDF parse tree transformer that converts Lark trees into data structures."""

from typing import TypeVar

from lark import Token, Transformer, v_args

from sdf_toolkit.core.model import (
    BaseEntry,
    DelayPaths,
    Device,
    EdgeType,
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
    Setup,
    SetupHold,
    TimingCheck,
    TimingPortSpec,
    Values,
    Width,
)

_TC = TypeVar("_TC", bound=TimingCheck)


def remove_quotation(s: str) -> str:
    """Remove quotation marks from string."""
    return s.replace('"', "")


def _format_value(val: float | None) -> str:
    """Format a single timing value, using integer form when possible."""
    if val is None:
        return ""
    if val == int(val):
        return str(int(val))
    return str(val)


def _format_values_triple(v: Values) -> str:
    """Format a Values object as an SDF triple string (e.g. '5.5:5.0:4.5')."""
    return ":".join(_format_value(val) for val in (v.min, v.avg, v.max))


class SDFTransformer(Transformer):
    """Transformer that processes the SDF parse tree into data structures."""

    def __init__(self) -> None:
        super().__init__()
        self.sdf_file_obj = SDFFile()
        self.delays_list: list[BaseEntry] = []

    # ── Top-level structure ──────────────────────────────────────────

    @v_args(inline=True)
    def sdf_file(self, _tag: Token, *items: dict[str, str]) -> SDFFile:
        """Process the top-level SDF file structure."""
        for item in items:
            if isinstance(item, dict):
                for key, value in item.items():
                    if hasattr(self.sdf_file_obj.header, key):
                        setattr(self.sdf_file_obj.header, key, value)
        return self.sdf_file_obj

    @v_args(inline=True)
    def sdf_item(self, item: dict[str, str]) -> dict[str, str]:
        """Process individual SDF items (header or cell)."""
        return item

    @v_args(inline=True)
    def sdf_header_item(self, item: dict[str, str]) -> dict[str, str]:
        """Pass through header items."""
        return item

    # ── Header fields ────────────────────────────────────────────────

    @v_args(inline=True)
    def sdf_version(self, value: Token | None = None) -> dict[str, str]:
        """Process SDF version header field."""
        return {"sdfversion": remove_quotation(str(value)) if value else ""}

    @v_args(inline=True)
    def design(self, value: Token | None = None) -> dict[str, str]:
        """Process design name header field."""
        return {"design": remove_quotation(str(value)) if value else ""}

    @v_args(inline=True)
    def date(self, value: Token | None = None) -> dict[str, str]:
        """Process date header field."""
        return {"date": remove_quotation(str(value)) if value else ""}

    @v_args(inline=True)
    def vendor(self, value: Token | None = None) -> dict[str, str]:
        """Process vendor header field."""
        return {"vendor": remove_quotation(str(value)) if value else ""}

    @v_args(inline=True)
    def program(self, value: Token | None = None) -> dict[str, str]:
        """Process program header field."""
        return {"program": remove_quotation(str(value)) if value else ""}

    @v_args(inline=True)
    def version(self, value: Token | None = None) -> dict[str, str]:
        """Process version header field."""
        return {"version": remove_quotation(str(value)) if value else ""}

    @v_args(inline=True)
    def process(self, value: Token | None = None) -> dict[str, str]:
        """Process process header field."""
        return {"process": remove_quotation(str(value)) if value else ""}

    @v_args(inline=True)
    def voltage(self, val: Values) -> dict[str, str]:
        """Process voltage specification."""
        return {"voltage": _format_values_triple(val)}

    @v_args(inline=True)
    def temperature(self, val: Values) -> dict[str, str]:
        """Process temperature specification."""
        return {"temperature": _format_values_triple(val)}

    @v_args(inline=True)
    def hierarchy_divider(self, val: Token) -> dict[str, str]:
        """Process hierarchy divider."""
        return {"divider": str(val)}

    @v_args(inline=True)
    def timescale(self, val: float, unit: Token) -> dict[str, str]:
        """Process timescale specification."""
        int_val = int(val)
        val_str = str(int_val) if val == int_val else str(val)
        return {"timescale": val_str + str(unit)}

    # ── Value processing ─────────────────────────────────────────────

    @v_args(inline=True)
    def rvalue(self, *args: float | Values | Token | None) -> Values:
        """Process single value or real triple."""
        for arg in args:
            if isinstance(arg, Values):
                return arg
            if isinstance(arg, float):
                return Values(min=arg, avg=arg, max=arg)
            if isinstance(arg, Token) and arg.type == "FLOAT":
                value = float(arg)
                return Values(min=value, avg=value, max=value)
        return Values()

    @v_args(inline=True)
    def real_triple(self, min_val: Token, avg_val: Token, max_val: Token) -> Values:
        """Process real triple (min:avg:max)."""
        return Values(
            min=float(min_val) if min_val is not None else None,
            avg=float(avg_val) if avg_val is not None else None,
            max=float(max_val) if max_val is not None else None,
        )

    @v_args(inline=True)
    def delval_list(self, *items: Values | None) -> DelayPaths:
        """Process delay value list (1, 2, or 3 real triples)."""
        valid_items = [item for item in items if isinstance(item, Values)]

        if len(valid_items) == 1:
            return DelayPaths(nominal=valid_items[0])
        if len(valid_items) == 2:
            return DelayPaths(fast=valid_items[0], slow=valid_items[1])
        if len(valid_items) == 3:
            return DelayPaths(
                fast=valid_items[0], nominal=valid_items[1], slow=valid_items[2]
            )
        return DelayPaths(nominal=Values())

    # ── Cell structure ───────────────────────────────────────────────

    @v_args(inline=True)
    def cell(
        self,
        celltype: str,
        instance: str | None,
        delays: None = None,
    ) -> dict[str, str]:
        """Process individual cell definition."""
        inst = str(instance) if instance is not None else ""
        self._add_cell(str(celltype), inst)
        if delays is not None:
            self._add_delays_to_cell(str(celltype), inst, self.delays_list)
        self.delays_list = []
        return {}

    @v_args(inline=True)
    def celltype(self, val: Token) -> str:
        """Process cell type."""
        return remove_quotation(str(val))

    @v_args(inline=True)
    def instance(self, val: Token | None = None) -> str | None:
        """Process instance name."""
        if val is None:
            return None
        return str(val)

    # ── Delay blocks ─────────────────────────────────────────────────

    @v_args(inline=True)
    def delay_entry(self, item: BaseEntry) -> BaseEntry:
        """Unwrap delay entry."""
        return item

    def _collect_delays(
        self, delays: tuple[BaseEntry | tuple[BaseEntry, ...], ...], *, flag: str
    ) -> None:
        """Flatten delay entries and set the given boolean flag on each."""
        for d in delays:
            if isinstance(d, tuple):
                for sub_d in d:
                    if isinstance(sub_d, BaseEntry):
                        setattr(sub_d, flag, True)
                        self.delays_list.append(sub_d)
            elif isinstance(d, BaseEntry):
                setattr(d, flag, True)
                self.delays_list.append(d)

    @v_args(inline=True)
    def absolute(self, *delays: BaseEntry | tuple[BaseEntry, ...]) -> None:
        """Process absolute delay block."""
        self._collect_delays(delays, flag="is_absolute")

    @v_args(inline=True)
    def increment(self, *delays: BaseEntry | tuple[BaseEntry, ...]) -> None:
        """Process increment delay block."""
        self._collect_delays(delays, flag="is_incremental")

    # ── Delay types ──────────────────────────────────────────────────

    @v_args(inline=True)
    def iopath(
        self,
        input_port: PortSpec,
        output_port: PortSpec,
        delay_values: DelayPaths,
    ) -> Iopath:
        """Process IOPATH delay specification."""
        return Iopath(
            name=f"iopath_{input_port.port}_{output_port.port}",
            from_pin=input_port.port,
            to_pin=output_port.port,
            from_pin_edge=input_port.port_edge,
            to_pin_edge=output_port.port_edge,
            delay_paths=delay_values,
        )

    @v_args(inline=True)
    def interconnect(
        self,
        input_port: PortSpec,
        output_port: PortSpec,
        delay_values: DelayPaths,
    ) -> Interconnect:
        """Process INTERCONNECT delay specification."""
        return Interconnect(
            name=f"interconnect_{input_port.port}_{output_port.port}",
            from_pin=input_port.port,
            to_pin=output_port.port,
            from_pin_edge=input_port.port_edge,
            to_pin_edge=output_port.port_edge,
            delay_paths=delay_values,
        )

    @v_args(inline=True)
    def port(self, port_spec: PortSpec, delay_values: DelayPaths) -> Port:
        """Process PORT delay specification."""
        return Port(
            name=f"port_{port_spec.port}",
            from_pin=port_spec.port,
            to_pin=port_spec.port,
            delay_paths=delay_values,
        )

    @v_args(inline=True)
    def device(self, port_spec: PortSpec, delay_values: DelayPaths) -> Device:
        """Process DEVICE delay specification."""
        return Device(
            name=f"device_{port_spec.port}",
            from_pin=port_spec.port,
            to_pin=port_spec.port,
            delay_paths=delay_values,
        )

    # ── Port specification ───────────────────────────────────────────

    @v_args(inline=True)
    def port_spec(self, *args: Token) -> PortSpec:
        """Process port specification."""
        if len(args) == 1:
            return PortSpec(port=str(args[0]), port_edge=None)
        if len(args) == 2:
            return PortSpec(port=str(args[1]), port_edge=EdgeType(str(args[0]).lower()))
        raise ValueError(f"Invalid port_spec args: {args}")

    @v_args(inline=True)
    def port_condition(self, token: Token) -> str:
        """Process port condition (posedge/negedge)."""
        return str(token)

    @v_args(inline=True)
    def timing_port(self, *args: PortSpec | str) -> TimingPortSpec:
        """Process timing port with optional condition.

        Grammar: ``timing_port: port_spec | "(" "COND" equation port_spec ")"``

        Receives either 1 arg (bare port_spec) or 2 args (equation, port_spec).
        """
        if len(args) == 1:
            ps = args[0]
            if not isinstance(ps, PortSpec):
                raise TypeError(f"Expected PortSpec, got {type(ps).__name__}")
            return TimingPortSpec(port=ps.port, port_edge=ps.port_edge)

        if len(args) == 2:
            condition, ps = args
            if not isinstance(ps, PortSpec):
                raise TypeError(f"Expected PortSpec, got {type(ps).__name__}")
            cond_eq = str(condition) if condition else ""
            return TimingPortSpec(
                port=ps.port,
                port_edge=ps.port_edge,
                cond=True,
                cond_equation=cond_eq,
            )

        raise ValueError(f"Invalid timing_port args: {args}")

    # ── Timing checks ────────────────────────────────────────────────

    def _make_timing_check(
        self,
        cls: type[_TC],
        to_port: TimingPortSpec,
        from_port: TimingPortSpec,
        paths: DelayPaths,
    ) -> _TC:
        """Build a timing check entry from port specs and delay paths."""
        return cls(
            name=f"{cls.__name__.lower()}_{from_port.port}_{to_port.port}",
            is_timing_check=True,
            is_cond=from_port.cond,
            cond_equation=from_port.cond_equation,
            from_pin=from_port.port,
            to_pin=to_port.port,
            from_pin_edge=from_port.port_edge,
            to_pin_edge=to_port.port_edge,
            delay_paths=paths,
        )

    @v_args(inline=True)
    def setup_check(
        self, to_port: TimingPortSpec, from_port: TimingPortSpec, values: Values
    ) -> Setup:
        """Process setup timing check."""
        paths = DelayPaths(nominal=values)
        return self._make_timing_check(Setup, to_port, from_port, paths)

    @v_args(inline=True)
    def hold_check(
        self, to_port: TimingPortSpec, from_port: TimingPortSpec, values: Values
    ) -> Hold:
        """Process hold timing check."""
        paths = DelayPaths(nominal=values)
        return self._make_timing_check(Hold, to_port, from_port, paths)

    @v_args(inline=True)
    def removal_check(
        self, to_port: TimingPortSpec, from_port: TimingPortSpec, values: Values
    ) -> Removal:
        """Process removal timing check."""
        paths = DelayPaths(nominal=values)
        return self._make_timing_check(Removal, to_port, from_port, paths)

    @v_args(inline=True)
    def recovery_check(
        self, to_port: TimingPortSpec, from_port: TimingPortSpec, values: Values
    ) -> Recovery:
        """Process recovery timing check."""
        paths = DelayPaths(nominal=values)
        return self._make_timing_check(Recovery, to_port, from_port, paths)

    @v_args(inline=True)
    def width_check(self, port: TimingPortSpec, values: Values) -> Width:
        """Process width timing check."""
        paths = DelayPaths(nominal=values)
        return self._make_timing_check(Width, port, port, paths)

    @v_args(inline=True)
    def period_check(self, port: TimingPortSpec, values: Values) -> Period:
        """Process period timing check."""
        paths = DelayPaths(nominal=values)
        return self._make_timing_check(Period, port, port, paths)

    @v_args(inline=True)
    def setuphold_check(
        self,
        to_port: TimingPortSpec,
        from_port: TimingPortSpec,
        setup_val: Values,
        hold_val: Values,
    ) -> SetupHold:
        """Process setuphold timing check."""
        paths = DelayPaths(setup=setup_val, hold=hold_val)
        return self._make_timing_check(SetupHold, to_port, from_port, paths)

    @v_args(inline=True)
    def t_check(self, item: BaseEntry) -> BaseEntry:
        """Pass through timing check entry."""
        return item

    @v_args(inline=True)
    def timing_check_list(self, *items: BaseEntry) -> None:
        """Process timing check list."""
        self.delays_list.extend(items)

    # ── Conditional delays ───────────────────────────────────────────

    @v_args(inline=True)
    def cond_delay(self, condition: str, *delays: BaseEntry) -> tuple[BaseEntry, ...]:
        """Process conditional delay."""
        for delay in delays:
            delay.is_cond = True
            delay.cond_equation = condition
        return delays

    @v_args(inline=True)
    def delay_condition(self, *args: str | Token) -> str:
        """Handle delay condition."""
        for arg in args:
            if isinstance(arg, str) and arg not in ("(", ")"):
                return arg
        return "".join(str(a) for a in args if str(a) not in ("(", ")"))

    @v_args(inline=True)
    def equation(self, *items: str | Token) -> str:
        """Process equation for conditions."""
        return " ".join(str(item) for item in items)

    @v_args(inline=True)
    def equation_item(self, item: Token) -> str:
        """Process equation items."""
        return str(item)

    # ── Constraints ──────────────────────────────────────────────────

    @v_args(inline=True)
    def path_constraint(
        self,
        to_port: PortSpec,
        from_port: PortSpec,
        rise_val: Values,
        fall_val: Values,
    ) -> PathConstraint:
        """Process path constraint."""
        paths = DelayPaths(rise=rise_val, fall=fall_val)
        return PathConstraint(
            name=f"pathconstraint_{from_port.port}_{to_port.port}",
            is_timing_env=True,
            from_pin=from_port.port,
            to_pin=to_port.port,
            from_pin_edge=from_port.port_edge,
            to_pin_edge=to_port.port_edge,
            delay_paths=paths,
        )

    @v_args(inline=True)
    def constraints_list(self, *items: BaseEntry) -> None:
        """Process constraints list."""
        self.delays_list.extend(items)

    # ── Helpers ──────────────────────────────────────────────────────

    def _add_cell(self, name: str, instance: str) -> None:
        """Add cell to cells dictionary."""
        self.sdf_file_obj.cells.setdefault(name, {}).setdefault(instance, {})

    def _add_delays_to_cell(
        self,
        celltype: str,
        instance: str,
        delays: list[BaseEntry],
    ) -> None:
        """Add delays to a cell, appending _1, _2, etc. on name collision."""
        cell_dict = self.sdf_file_obj.cells[celltype][instance]
        for entry in delays:
            base_name = entry.name
            key = base_name
            if key in cell_dict:
                counter = 1
                while f"{base_name}_{counter}" in cell_dict:
                    counter += 1
                key = f"{base_name}_{counter}"
                entry.name = key
            cell_dict[key] = entry

    # ── Terminal values ──────────────────────────────────────────────

    @v_args(inline=True)
    def STRING(self, value: Token) -> str:  # noqa: N802
        """Convert STRING token to str."""
        return str(value)

    @v_args(inline=True)
    def FLOAT(self, value: Token) -> float:  # noqa: N802
        """Convert FLOAT token to float."""
        return float(value)

    @v_args(inline=True)
    def QSTRING(self, value: Token) -> str:  # noqa: N802
        """Convert QSTRING token to str."""
        return str(value)

    @v_args(inline=True)
    def QFLOAT(self, value: Token) -> str:  # noqa: N802
        """Convert QFLOAT token to str."""
        return str(value)

    @v_args(inline=True)
    def operator(self, token: Token) -> str:
        """Return operator string."""
        return str(token)
