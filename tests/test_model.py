"""Tests for model.py: header, values, delay transitions, entries and JSON form."""

import pytest

from sdf_toolkit.core.model import (
    ArcKey,
    Cond,
    CondElse,
    Corner,
    DelayMode,
    DelayValues,
    Device,
    Divider,
    EdgeSelect,
    EdgeType,
    Entry,
    EntryType,
    HeaderField,
    Interconnect,
    Iopath,
    MissingDelayError,
    PathConstraint,
    Period,
    Port,
    PortSpec,
    SDFFile,
    SDFHeader,
    Setup,
    SetupHold,
    Timescale,
    TimeUnit,
    TimingPortSpec,
    Transition,
    Values,
    Width,
    entry_from_dict,
    entry_to_dict,
)


def _v(value: float) -> Values:
    return Values.uniform(value)


def _delay(count: int) -> DelayValues:
    """A delay whose n-th rvalue is n + 1, so positions are readable."""
    return DelayValues(rvalues=tuple(_v(i + 1) for i in range(count)))


class TestSDFHeader:
    def test_to_dict_filters_none(self):
        header = SDFHeader(sdfversion="2.1", design="top")
        assert header.to_dict() == {
            HeaderField.SDFVERSION: "2.1",
            HeaderField.DESIGN: "top",
        }

    def test_to_dict_writes_typed_fields_as_sdf(self):
        header = SDFHeader(divider=Divider.SLASH, timescale=Timescale.parse("10ps"))
        assert header.to_dict() == {
            HeaderField.DIVIDER: "/",
            HeaderField.TIMESCALE: "10ps",
        }

    def test_from_dict_round_trip(self):
        header = SDFHeader(
            sdfversion="3.0", divider=Divider.SLASH, timescale=Timescale.parse("1ns")
        )
        assert SDFHeader.from_dict(header.to_dict()) == header

    @pytest.mark.parametrize(
        ("header", "timescale", "divider"),
        [
            (SDFHeader(), Timescale(value=1, unit=TimeUnit.NS), Divider.DOT),
            (
                SDFHeader(timescale=Timescale.parse("100ps"), divider=Divider.SLASH),
                Timescale(value=100, unit=TimeUnit.PS),
                Divider.SLASH,
            ),
        ],
    )
    def test_effective_fields_apply_spec_defaults(self, header, timescale, divider):
        assert header.effective_timescale == timescale
        assert header.effective_divider == divider

    def test_merged_later_fields_win(self):
        merged = SDFHeader(design="a", vendor="v").merged(SDFHeader(design="b"))
        assert merged == SDFHeader(design="b", vendor="v")


class TestTimescale:
    @pytest.mark.parametrize(
        ("text", "value", "unit", "femtoseconds"),
        [
            ("1ns", 1, TimeUnit.NS, 1_000_000),
            ("10 ps", 10, TimeUnit.PS, 10_000),
            ("100us", 100, TimeUnit.US, 100_000_000_000),
            ("1.0fs", 1, TimeUnit.FS, 1),
        ],
    )
    def test_parse(self, text, value, unit, femtoseconds):
        timescale = Timescale.parse(text)
        assert (timescale.value, timescale.unit) == (value, unit)
        assert timescale.femtoseconds == femtoseconds

    @pytest.mark.parametrize("text", ["", "ns", "2ns", "1 parsec", "1ns trailing"])
    def test_parse_rejects(self, text):
        with pytest.raises(ValueError, match="timescale"):
            Timescale.parse(text)

    def test_str(self):
        assert str(Timescale.parse("10 ps")) == "10ps"


class TestValues:
    def test_to_dict(self):
        v = Values(min=1.0, typ=2.0, max=3.0)
        assert v.to_dict() == {"min": 1.0, "typ": 2.0, "max": 3.0}

    def test_to_dict_with_nones(self):
        assert Values(typ=5.0).to_dict() == {"min": None, "typ": 5.0, "max": None}

    @pytest.mark.parametrize(
        ("corner", "expected"),
        [(Corner.MIN, 1.0), (Corner.TYP, 2.0), (Corner.MAX, 3.0)],
    )
    def test_at(self, corner, expected):
        assert Values(min=1.0, typ=2.0, max=3.0).at(corner) == expected

    def test_frozen(self):
        v = Values(min=1.0)
        with pytest.raises(AttributeError):
            v.min = 2.0  # type: ignore[misc]


class TestDelayValuesCount:
    @pytest.mark.parametrize("count", [1, 2, 3, 6, 12])
    def test_valid_counts(self, count):
        assert len(_delay(count).rvalues) == count

    @pytest.mark.parametrize("count", [0, 4, 5, 7, 11, 13])
    def test_invalid_counts_raise(self, count):
        with pytest.raises(ValueError, match="rvalues"):
            _delay(count)


# Expected rvalue position (1-based) per transition for each count, per
# IEEE 1497: 2 values are rise, fall; 3 add turn-off; 6 and 12 list
# the transitions in order.
_TRANSITION_TABLE: dict[int, dict[Transition, int]] = {
    1: dict.fromkeys(list(Transition)[:6], 1),
    2: {
        Transition.T01: 1,
        Transition.T10: 2,
        Transition.T0Z: 1,
        Transition.TZ1: 1,
        Transition.T1Z: 2,
        Transition.TZ0: 2,
    },
    3: {
        Transition.T01: 1,
        Transition.T10: 2,
        Transition.T0Z: 3,
        Transition.TZ1: 1,
        Transition.T1Z: 3,
        Transition.TZ0: 2,
    },
    6: {t: i + 1 for i, t in enumerate(list(Transition)[:6])},
    12: {t: i + 1 for i, t in enumerate(Transition)},
}


class TestForTransition:
    @pytest.mark.parametrize(
        ("count", "transition", "position"),
        [
            (count, transition, position)
            for count, table in _TRANSITION_TABLE.items()
            for transition, position in table.items()
        ],
    )
    def test_expansion(self, count, transition, position):
        assert _delay(count).for_transition(transition) == _v(position)

    @pytest.mark.parametrize("count", [1, 2, 3, 6])
    @pytest.mark.parametrize(
        "transition",
        [Transition.T0X, Transition.TX1, Transition.TXZ, Transition.TZX],
    )
    def test_x_transition_needs_twelve(self, count, transition):
        with pytest.raises(ValueError, match="12 rvalues"):
            _delay(count).for_transition(transition)


class TestScalar:
    @pytest.mark.parametrize(
        ("edge", "expected"),
        [
            (EdgeSelect.RISE, 1.0),
            (EdgeSelect.FALL, 2.0),
            (EdgeSelect.WORST, 3.0),
            (EdgeSelect.BEST, 1.0),
        ],
    )
    def test_edge_selection(self, edge, expected):
        assert _delay(3).scalar(corner=Corner.MAX, edge=edge) == expected

    @pytest.mark.parametrize(
        ("delay", "corner", "edge"),
        [
            (DelayValues.of(Values()), Corner.MAX, EdgeSelect.WORST),
            (DelayValues.of(Values(min=1.0)), Corner.MAX, EdgeSelect.WORST),
            (DelayValues.of(Values(max=1.0), Values()), Corner.MAX, EdgeSelect.FALL),
            (DelayValues.of(Values(max=1.0)), Corner.TYP, EdgeSelect.BEST),
        ],
    )
    def test_missing_value_raises(self, delay, corner, edge):
        with pytest.raises(MissingDelayError):
            delay.scalar(corner=corner, edge=edge)

    def test_missing_delay_error_is_value_error(self):
        assert issubclass(MissingDelayError, ValueError)


_PIN_A = PortSpec("A")
_PIN_Y = PortSpec("Y")
_POS_CLK = TimingPortSpec("CLK", edge=EdgeType.POSEDGE)
_DATA_EN = TimingPortSpec("D", cond=Cond("EN == 1'b1", name="en"))

_ENTRIES: list[Entry] = [
    Iopath(
        input=PortSpec("A", edge=EdgeType.POSEDGE),
        output=_PIN_Y,
        mode=DelayMode.ABSOLUTE,
        delay=_delay(2),
        cond=Cond("B == 1'b0"),
    ),
    Iopath(
        input=_PIN_A,
        output=_PIN_Y,
        mode=DelayMode.INCREMENT,
        delay=_delay(6),
        cond=CondElse(),
    ),
    Port(port=_PIN_A, mode=DelayMode.ABSOLUTE, delay=_delay(1)),
    Interconnect(
        source=PortSpec("u1.Y"),
        load=PortSpec("u2.A"),
        mode=DelayMode.ABSOLUTE,
        delay=_delay(3),
    ),
    Device(output=None, mode=DelayMode.ABSOLUTE, delay=_delay(12)),
    Setup(data=_DATA_EN, reference=_POS_CLK, limit=_v(1)),
    SetupHold(data=_DATA_EN, reference=_POS_CLK, setup=_v(1), hold=_v(2)),
    Width(port=_POS_CLK, limit=Values(min=1.0, max=2.0)),
    Period(port=_POS_CLK, limit=_v(4)),
    PathConstraint(ports=("a.Y", "b.A"), rise=_v(1), fall=_v(2)),
]


class TestEntries:
    @pytest.mark.parametrize("entry", _ENTRIES, ids=lambda e: str(e.type))
    def test_dict_round_trip(self, entry):
        assert entry_from_dict(entry_to_dict(entry)) == entry

    def test_key_separates_edges(self):
        rise = Iopath(
            input=PortSpec("CLK", edge=EdgeType.POSEDGE),
            output=_PIN_Y,
            mode=DelayMode.ABSOLUTE,
            delay=_delay(1),
        )
        fall = Iopath(
            input=PortSpec("CLK", edge=EdgeType.NEGEDGE),
            output=_PIN_Y,
            mode=DelayMode.ABSOLUTE,
            delay=_delay(1),
        )
        assert rise.key != fall.key

    def test_key_ignores_values(self):
        first = Setup(data=_DATA_EN, reference=_POS_CLK, limit=_v(1))
        second = Setup(data=_DATA_EN, reference=_POS_CLK, limit=_v(9))
        assert first.key == second.key

    def test_check_key_keeps_data_port_cond(self):
        entry = Setup(data=_DATA_EN, reference=_POS_CLK, limit=_v(1))
        assert entry.key == ArcKey(
            type=EntryType.SETUP, pins=(_DATA_EN, _POS_CLK), cond=None, mode=None
        )
        assert str(entry.key) == "SETUP (COND EN == 1'b1 D) (posedge CLK)"

    def test_key_separates_scond(self):
        data = TimingPortSpec("D")
        plain = SetupHold(data=data, reference=_POS_CLK, setup=_v(1), hold=_v(2))
        stamped = SetupHold(
            data=data,
            reference=_POS_CLK,
            setup=_v(1),
            hold=_v(2),
            scond=Cond(equation="EN"),
        )
        assert plain.key != stamped.key
        assert str(stamped.key) == "SETUPHOLD D (posedge CLK) SCOND(EN)"

    def test_entries_are_frozen(self):
        entry = Width(port=_POS_CLK, limit=_v(1))
        with pytest.raises(AttributeError):
            entry.limit = _v(2)  # type: ignore[misc]


class TestSDFFile:
    def test_to_dict_from_dict_round_trip(self):
        sdf = SDFFile(
            header=SDFHeader(sdfversion="3.0", timescale=Timescale.parse("1ps")),
            cells={"INV": {"inst1": list(_ENTRIES)}},
        )
        assert SDFFile.from_dict(sdf.to_dict()) == sdf

    def test_to_dict_layout(self):
        sdf = SDFFile(
            header=SDFHeader(sdfversion="2.1"),
            cells={"INV": {"inst1": [_ENTRIES[2]]}},
        )
        d = sdf.to_dict()
        assert d["header"] == {"sdfversion": "2.1"}
        assert d["cells"]["INV"]["inst1"][0]["type"] == "port"
