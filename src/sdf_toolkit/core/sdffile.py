"""The SDF file, its header, LABEL entries, and the dictionary form of each entry.

{class}`SDFFile` keeps each cell instance's entries as a list in file order.
{func}`entry_to_dict` and {func}`entry_from_dict` sit in this module because
{meth}`SDFFile.to_dict` and {meth}`SDFFile.from_dict` call them and they
dispatch over every member of {data}`Entry`, LABEL included.
"""

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar

from sdf_toolkit.core.checks import (
    BidirectSkew,
    Hold,
    NoChange,
    Period,
    Recovery,
    Recrem,
    Removal,
    Setup,
    SetupHold,
    Skew,
    TimingCheck,
    Width,
)
from sdf_toolkit.core.delays import (
    ArcKey,
    Cond,
    CondElse,
    DelayEntry,
    Device,
    Interconnect,
    Iopath,
    NetDelay,
    PathPulse,
    PathPulsePercent,
    Port,
    PortSpec,
    PulseEntry,
    Retain,
    TimingPortSpec,
)
from sdf_toolkit.core.enums import DelayMode, Divider, EdgeType, EntryType, HeaderField
from sdf_toolkit.core.timingenv import (
    Arrival,
    ConstraintPath,
    Departure,
    DiffConstraint,
    PathConstraint,
    PeriodConstraint,
    SkewConstraint,
    Slack,
    SumConstraint,
    TimingEnvEntry,
    Waveform,
    WaveformEdge,
)
from sdf_toolkit.core.values import (
    DEFAULT_TIMESCALE,
    DelayValues,
    Number,
    PulseLimits,
    Timescale,
    Values,
)

DEFAULT_DIVIDER = Divider.DOT


@dataclass(frozen=True, kw_only=True)
class Label:
    """A ``LABEL`` value: the delay a model variable such as a specparam takes.

    *name* is the identifier of the variable, not a port.
    """

    name: str
    mode: DelayMode
    delay: DelayValues

    type: ClassVar[EntryType] = EntryType.LABEL

    @property
    def key(self) -> ArcKey:
        """Identity of the label: its variable and mode."""
        return ArcKey(
            type=self.type, pins=(), cond=None, mode=self.mode, name=self.name
        )


Entry = DelayEntry | PulseEntry | TimingCheck | TimingEnvEntry | Label


# Cell type -> instance -> entries in file order.
CellsDict = dict[str, dict[str, list[Entry]]]


def group_by_key(entries: Iterable[Entry]) -> dict[ArcKey, list[Entry]]:
    """Group *entries* by arc, keeping file order within each group."""
    groups: dict[ArcKey, list[Entry]] = {}
    for entry in entries:
        groups.setdefault(entry.key, []).append(entry)
    return groups


def _delay_labels(delay: DelayValues, name: str = "delay") -> list[tuple[str, Values]]:
    labels = [(f"{name}[{i}]", v) for i, v in enumerate(delay.rvalues)]
    for i, limits in enumerate(delay.limits or ()):
        if limits is not None:
            labels.append((f"{name}[{i}].reject", limits.reject))
            if limits.error is not None:
                labels.append((f"{name}[{i}].error", limits.error))
    return labels


def labelled_values(entry: Entry) -> list[tuple[str, Values]]:
    """Return every value triple of *entry*, labelled by the field it fills.

    Delay rvalues read ``delay[i]`` in file order; checks read ``limit`` or
    their two named limits; a path constraint reads ``rise`` and ``fall``.
    """
    match entry:
        case Iopath():
            return _delay_labels(entry.delay) + [
                label
                for k, retain in enumerate(entry.retain)
                for label in _delay_labels(retain.delay, f"retain[{k}]")
            ]
        case Port() | Interconnect() | Device() | NetDelay() | Label():
            return _delay_labels(entry.delay)
        case PathPulse() | PathPulsePercent():
            error = [] if entry.error is None else [("error", entry.error)]
            return [("reject", entry.reject), *error]
        case Setup() | Hold() | Recovery() | Removal() | Skew() | Width() | Period():
            return [("limit", entry.limit)]
        case SetupHold():
            return [("setup", entry.setup), ("hold", entry.hold)]
        case Recrem():
            return [("recovery", entry.recovery), ("removal", entry.removal)]
        case BidirectSkew():
            return [
                ("first_limit", entry.first_limit),
                ("second_limit", entry.second_limit),
            ]
        case NoChange():
            return [("setup", entry.setup), ("hold", entry.hold)]
        case PathConstraint():
            return [("rise", entry.rise), ("fall", entry.fall)]
        case SumConstraint() | DiffConstraint():
            fall = [] if entry.fall is None else [("fall", entry.fall)]
            return [("rise", entry.rise), *fall]
        case PeriodConstraint() | SkewConstraint():
            return [("limit", entry.limit)]
        case Arrival() | Departure():
            return [
                ("earliest_rise", entry.earliest_rise),
                ("latest_rise", entry.latest_rise),
                ("earliest_fall", entry.earliest_fall),
                ("latest_fall", entry.latest_fall),
            ]
        case Slack():
            return [
                ("rise_setup", entry.rise_setup),
                ("fall_setup", entry.fall_setup),
                ("rise_hold", entry.rise_hold),
                ("fall_hold", entry.fall_hold),
            ]
        case Waveform():
            # Offsets and the period are plain numbers, not triples.
            return []


@dataclass(frozen=True)
class SDFHeader:
    """SDF header fields as the file writes them; None marks an absent field."""

    sdfversion: str | None = None
    design: str | None = None
    vendor: str | None = None
    program: str | None = None
    version: str | None = None
    divider: Divider | None = None
    date: str | None = None
    voltage: str | None = None
    process: str | None = None
    temperature: str | None = None
    timescale: Timescale | None = None

    @property
    def effective_timescale(self) -> Timescale:
        """The timescale in force: the file's, else the 1 ns IEEE 1497 default."""
        return self.timescale if self.timescale is not None else DEFAULT_TIMESCALE

    @property
    def effective_divider(self) -> Divider:
        """The divider in force: the file's, else the ``.`` IEEE 1497 default."""
        return self.divider if self.divider is not None else DEFAULT_DIVIDER

    def merged(self, later: "SDFHeader") -> "SDFHeader":
        """Return this header with every field *later* sets taking its place."""
        return SDFHeader(
            sdfversion=_later(self.sdfversion, later.sdfversion),
            design=_later(self.design, later.design),
            vendor=_later(self.vendor, later.vendor),
            program=_later(self.program, later.program),
            version=_later(self.version, later.version),
            divider=_later(self.divider, later.divider),
            date=_later(self.date, later.date),
            voltage=_later(self.voltage, later.voltage),
            process=_later(self.process, later.process),
            temperature=_later(self.temperature, later.temperature),
            timescale=_later(self.timescale, later.timescale),
        )

    def to_dict(self) -> dict[HeaderField, str]:
        """Return the fields the file sets, written as in SDF."""
        written: dict[HeaderField, str | None] = {
            HeaderField.SDFVERSION: self.sdfversion,
            HeaderField.DESIGN: self.design,
            HeaderField.VENDOR: self.vendor,
            HeaderField.PROGRAM: self.program,
            HeaderField.VERSION: self.version,
            HeaderField.DIVIDER: None if self.divider is None else str(self.divider),
            HeaderField.DATE: self.date,
            HeaderField.VOLTAGE: self.voltage,
            HeaderField.PROCESS: self.process,
            HeaderField.TEMPERATURE: self.temperature,
            HeaderField.TIMESCALE: (
                None if self.timescale is None else str(self.timescale)
            ),
        }
        return {k: v for k, v in written.items() if v is not None}

    @classmethod
    def from_dict(
        cls, data: Mapping[HeaderField, str] | Mapping[str, str]
    ) -> "SDFHeader":
        """Rebuild a header from {meth}`to_dict` output.

        Raises
        ------
        ValueError
            If a key is not a header field, or the divider or timescale is
            not valid SDF.
        """
        fields = {HeaderField(k): v for k, v in data.items()}
        divider = fields.pop(HeaderField.DIVIDER, None)
        timescale = fields.pop(HeaderField.TIMESCALE, None)
        return cls(
            sdfversion=fields.get(HeaderField.SDFVERSION),
            design=fields.get(HeaderField.DESIGN),
            vendor=fields.get(HeaderField.VENDOR),
            program=fields.get(HeaderField.PROGRAM),
            version=fields.get(HeaderField.VERSION),
            divider=None if divider is None else Divider(divider),
            date=fields.get(HeaderField.DATE),
            voltage=fields.get(HeaderField.VOLTAGE),
            process=fields.get(HeaderField.PROCESS),
            temperature=fields.get(HeaderField.TEMPERATURE),
            timescale=None if timescale is None else Timescale.parse(timescale),
        )


def _later[T](earlier: T | None, later: T | None) -> T | None:
    """Return *later* when it is set, else *earlier*."""
    return later if later is not None else earlier


@dataclass
class SDFFile:
    """Top-level SDF file: header and the entries of every cell instance."""

    header: SDFHeader = field(default_factory=SDFHeader)
    cells: CellsDict = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return the file as JSON-ready nested dictionaries and lists."""
        return {
            "header": {str(k): v for k, v in self.header.to_dict().items()},
            "cells": {
                cell_type: {
                    instance: [entry_to_dict(entry) for entry in entries]
                    for instance, entries in instances.items()
                }
                for cell_type, instances in self.cells.items()
            },
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SDFFile":
        """Rebuild a file from {meth}`to_dict` output.

        Raises
        ------
        ValueError
            If any part of *data* does not describe a valid entry or header.
        """
        return cls(
            header=SDFHeader.from_dict(data["header"]),
            cells={
                cell_type: {
                    instance: [entry_from_dict(e) for e in entries]
                    for instance, entries in instances.items()
                }
                for cell_type, instances in data["cells"].items()
            },
        )


def entry_to_dict(entry: Entry) -> dict[str, Any]:
    """Return *entry* as a JSON-ready dictionary tagged with its ``type``.

    A ``CONDELSE`` becomes an empty mapping, so it stays distinct from an
    absent condition, which becomes None.
    """
    return {"type": str(entry.type), **asdict(entry)}


def _values(data: Mapping[str, Number | None]) -> Values:
    return Values(min=data["min"], typ=data["typ"], max=data["max"])


def _delay(data: Mapping[str, Any]) -> DelayValues:
    limits = data["limits"]
    return DelayValues(
        rvalues=tuple(_values(v) for v in data["rvalues"]),
        limits=None
        if limits is None
        else tuple(
            None
            if lim is None
            else PulseLimits(
                reject=_values(lim["reject"]),
                error=None if lim["error"] is None else _values(lim["error"]),
            )
            for lim in limits
        ),
    )


def _port(data: Mapping[str, Any]) -> PortSpec:
    edge = data["edge"]
    return PortSpec(port=data["port"], edge=None if edge is None else EdgeType(edge))


def _cond(data: Mapping[str, Any] | None) -> Cond | None:
    return None if data is None else Cond(equation=data["equation"], name=data["name"])


def _timing_port(data: Mapping[str, Any]) -> TimingPortSpec:
    edge = data["edge"]
    return TimingPortSpec(
        port=data["port"],
        edge=None if edge is None else EdgeType(edge),
        cond=_cond(data["cond"]),
    )


def _delay_cond(data: Mapping[str, Any] | None) -> Cond | CondElse | None:
    if data is None:
        return None
    if not data:
        return CondElse()
    return _cond(data)


def _optional_values(data: Mapping[str, Number | None] | None) -> Values | None:
    return None if data is None else _values(data)


def _constraint_paths(
    data: Iterable[Mapping[str, str]],
) -> tuple[ConstraintPath, ...]:
    return tuple(ConstraintPath(start=p["start"], end=p["end"]) for p in data)


def _pulse_path(data: list[str] | None) -> tuple[str, str] | None:
    if data is None:
        return None
    source, sink = data
    return (source, sink)


def entry_from_dict(data: Mapping[str, Any]) -> Entry:
    """Rebuild an entry from {func}`entry_to_dict` output.

    Raises
    ------
    ValueError
        If ``type`` is unknown or a value is not valid SDF.
    KeyError
        If a field the entry type needs is missing.
    """
    entry_type = EntryType(data["type"])
    match entry_type:
        case EntryType.IOPATH:
            return Iopath(
                input=_port(data["input"]),
                output=_port(data["output"]),
                retain=tuple(Retain(delay=_delay(r["delay"])) for r in data["retain"]),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.PORT:
            return Port(
                port=_port(data["port"]),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.INTERCONNECT:
            return Interconnect(
                source=_port(data["source"]),
                load=_port(data["load"]),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.DEVICE:
            output = data["output"]
            return Device(
                output=None if output is None else _port(output),
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.NETDELAY:
            return NetDelay(
                net=data["net"],
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
                cond=_delay_cond(data["cond"]),
            )
        case EntryType.PATHPULSE | EntryType.PATHPULSEPERCENT:
            pulse_cls = (
                PathPulse if entry_type is EntryType.PATHPULSE else PathPulsePercent
            )
            return pulse_cls(
                path=_pulse_path(data["path"]),
                reject=_values(data["reject"]),
                error=_optional_values(data["error"]),
            )
        case EntryType.SETUP | EntryType.HOLD | EntryType.RECOVERY | EntryType.REMOVAL:
            check_cls = _LIMIT_CHECKS[entry_type]
            return check_cls(
                data=_timing_port(data["data"]),
                reference=_timing_port(data["reference"]),
                limit=_values(data["limit"]),
            )
        case EntryType.SETUPHOLD:
            return SetupHold(
                data=_timing_port(data["data"]),
                reference=_timing_port(data["reference"]),
                setup=_values(data["setup"]),
                hold=_values(data["hold"]),
                scond=_cond(data["scond"]),
                ccond=_cond(data["ccond"]),
            )
        case EntryType.RECREM:
            return Recrem(
                data=_timing_port(data["data"]),
                reference=_timing_port(data["reference"]),
                recovery=_values(data["recovery"]),
                removal=_values(data["removal"]),
                scond=_cond(data["scond"]),
                ccond=_cond(data["ccond"]),
            )
        case EntryType.SKEW:
            return Skew(
                stamp=_timing_port(data["stamp"]),
                check=_timing_port(data["check"]),
                limit=_values(data["limit"]),
            )
        case EntryType.BIDIRECTSKEW:
            return BidirectSkew(
                first=_timing_port(data["first"]),
                second=_timing_port(data["second"]),
                first_limit=_values(data["first_limit"]),
                second_limit=_values(data["second_limit"]),
            )
        case EntryType.NOCHANGE:
            return NoChange(
                control=_timing_port(data["control"]),
                data=_timing_port(data["data"]),
                setup=_values(data["setup"]),
                hold=_values(data["hold"]),
            )
        case EntryType.WIDTH:
            return Width(port=_timing_port(data["port"]), limit=_values(data["limit"]))
        case EntryType.PERIOD:
            return Period(port=_timing_port(data["port"]), limit=_values(data["limit"]))
        case EntryType.PATHCONSTRAINT:
            return PathConstraint(
                name=data["name"],
                ports=tuple(data["ports"]),
                rise=_values(data["rise"]),
                fall=_values(data["fall"]),
            )
        case EntryType.SUM:
            return SumConstraint(
                paths=_constraint_paths(data["paths"]),
                rise=_values(data["rise"]),
                fall=_optional_values(data["fall"]),
            )
        case EntryType.DIFF:
            return DiffConstraint(
                paths=_constraint_paths(data["paths"]),
                rise=_values(data["rise"]),
                fall=_optional_values(data["fall"]),
            )
        case EntryType.PERIODCONSTRAINT:
            return PeriodConstraint(
                port=data["port"],
                limit=_values(data["limit"]),
                exception=tuple(data["exception"]),
            )
        case EntryType.SKEWCONSTRAINT:
            return SkewConstraint(
                port=_port(data["port"]), limit=_values(data["limit"])
            )
        case EntryType.ARRIVAL | EntryType.DEPARTURE:
            env_cls = Arrival if entry_type is EntryType.ARRIVAL else Departure
            reference = data["reference"]
            return env_cls(
                reference=None if reference is None else _port(reference),
                port=data["port"],
                earliest_rise=_values(data["earliest_rise"]),
                latest_rise=_values(data["latest_rise"]),
                earliest_fall=_values(data["earliest_fall"]),
                latest_fall=_values(data["latest_fall"]),
            )
        case EntryType.SLACK:
            return Slack(
                port=data["port"],
                rise_setup=_values(data["rise_setup"]),
                fall_setup=_values(data["fall_setup"]),
                rise_hold=_values(data["rise_hold"]),
                fall_hold=_values(data["fall_hold"]),
                period=data["period"],
            )
        case EntryType.LABEL:
            return Label(
                name=data["name"],
                mode=DelayMode(data["mode"]),
                delay=_delay(data["delay"]),
            )
        case EntryType.WAVEFORM:
            return Waveform(
                port=data["port"],
                period=data["period"],
                edges=tuple(
                    WaveformEdge(edge=EdgeType(e["edge"]), offsets=tuple(e["offsets"]))
                    for e in data["edges"]
                ),
            )


_LIMIT_CHECKS: dict[EntryType, type[Setup | Hold | Recovery | Removal]] = {
    EntryType.SETUP: Setup,
    EntryType.HOLD: Hold,
    EntryType.RECOVERY: Recovery,
    EntryType.REMOVAL: Removal,
}
