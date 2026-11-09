"""Keyword vocabularies of OVI SDF 3.0 and IEEE 1497 as string enums.

Member values are lower case. {class}`CaseInsensitiveStrEnum` resolves the
upper-case spelling SDF files and CLI users write, so ``EntryType("IOPATH")``
returns ``EntryType.IOPATH``.
"""

from enum import StrEnum


class CaseInsensitiveStrEnum(StrEnum):
    """A ``StrEnum`` whose value lookup ignores case.

    SDF and Verilog keywords are conventionally written upper case (``IOPATH``,
    ``PORT``) while the member values here are lower case. This lets
    ``EntryType("IOPATH")`` resolve instead of raising, so CLI options that take
    these names work regardless of the case the user types.
    """

    @classmethod
    def _missing_(cls, value: object) -> "CaseInsensitiveStrEnum | None":
        """Resolve a value case-insensitively, or return None if unknown."""
        if isinstance(value, str):
            lowered = value.lower()
            for member in cls:
                if member.value == lowered:
                    return member
        return None


class EntryType(CaseInsensitiveStrEnum):
    """Types of SDF timing entries.

    Member values are lower case but lookup is case-insensitive, so the
    upper-case spellings used in SDF files resolve directly:

    >>> EntryType("IOPATH") is EntryType.IOPATH
    True
    >>> EntryType("Port") is EntryType.PORT
    True
    """

    PORT = "port"
    INTERCONNECT = "interconnect"
    IOPATH = "iopath"
    DEVICE = "device"
    NETDELAY = "netdelay"
    PATHPULSE = "pathpulse"
    PATHPULSEPERCENT = "pathpulsepercent"
    SETUP = "setup"
    HOLD = "hold"
    REMOVAL = "removal"
    RECOVERY = "recovery"
    WIDTH = "width"
    PERIOD = "period"
    SETUPHOLD = "setuphold"
    RECREM = "recrem"
    SKEW = "skew"
    BIDIRECTSKEW = "bidirectskew"
    NOCHANGE = "nochange"
    PATHCONSTRAINT = "pathconstraint"
    SUM = "sum"
    DIFF = "diff"
    PERIODCONSTRAINT = "periodconstraint"
    SKEWCONSTRAINT = "skewconstraint"
    ARRIVAL = "arrival"
    DEPARTURE = "departure"
    SLACK = "slack"
    WAVEFORM = "waveform"
    LABEL = "label"


class EdgeType(StrEnum):
    """Port edge identifiers in SDF port specifications.

    ``posedge`` and ``negedge`` cover every rising or falling transition,
    X included; the other six name one transition each.
    """

    POSEDGE = "posedge"
    NEGEDGE = "negedge"
    E01 = "01"
    E10 = "10"
    E0Z = "0z"
    EZ1 = "z1"
    E1Z = "1z"
    EZ0 = "z0"


class Corner(StrEnum):
    """Process corner, the position inside a ``(min:typ:max)`` triple."""

    MIN = "min"
    TYP = "typ"
    MAX = "max"


class Transition(StrEnum):
    """Signal transition, in the order SDF assigns rvalues to them."""

    T01 = "01"
    T10 = "10"
    T0Z = "0z"
    TZ1 = "z1"
    T1Z = "1z"
    TZ0 = "z0"
    T0X = "0x"
    TX1 = "x1"
    T1X = "1x"
    TX0 = "x0"
    TXZ = "xz"
    TZX = "zx"


class EdgeSelect(StrEnum):
    """How {meth}`DelayValues.scalar` reduces the rvalues to one number.

    ``RISE`` reads transition 01 and ``FALL`` reads transition 10.  ``WORST``
    takes the largest and ``BEST`` the smallest value present at the corner
    across every rvalue.
    """

    RISE = "rise"
    FALL = "fall"
    WORST = "worst"
    BEST = "best"


class DelayMode(StrEnum):
    """Whether a delay replaces (ABSOLUTE) or adds to (INCREMENT) a prior one."""

    ABSOLUTE = "absolute"
    INCREMENT = "increment"


class TimeUnit(StrEnum):
    """Unit of an SDF ``TIMESCALE``."""

    S = "s"
    MS = "ms"
    US = "us"
    NS = "ns"
    PS = "ps"
    FS = "fs"


class Divider(StrEnum):
    """Hierarchy divider character of an SDF ``DIVIDER``."""

    DOT = "."
    SLASH = "/"


class HeaderField(StrEnum):
    """SDF header field names."""

    SDFVERSION = "sdfversion"
    DESIGN = "design"
    VENDOR = "vendor"
    PROGRAM = "program"
    VERSION = "version"
    DIVIDER = "divider"
    DATE = "date"
    VOLTAGE = "voltage"
    PROCESS = "process"
    TEMPERATURE = "temperature"
    TIMESCALE = "timescale"
