"""SDF standards the parser can hold a file to, and the options of one parse.

One superset grammar reads both OVI SDF 3.0 and IEEE 1497, and the
transformer rejects what the chosen standard forbids as each rule reduces.
Only the reduction sees the raw text a rule needs, such as the delval count
of a ``RETAIN`` before padding, the entries one ``COND`` wraps and the token
positions for the error.
"""

from dataclasses import dataclass
from enum import StrEnum

# The version strings P1497 Draft 0.10 lists for SDFVERSION, in its order.
_VERSION_STRINGS = ("1.0", "2.0", "2.1", "3.0", "4.0")


class SDFStandard(StrEnum):
    """An SDF standard, named by its CLI spelling."""

    OVI_3_0 = "ovi-3.0"
    IEEE_1497 = "ieee-1497"

    @property
    def full_name(self) -> str:
        """Name the standard as its document does."""
        match self:
            case SDFStandard.OVI_3_0:
                return "OVI SDF 3.0"
            case SDFStandard.IEEE_1497:
                return "IEEE 1497 (SDF 4.0)"

    @property
    def version(self) -> str:
        """Return the SDFVERSION string that names this standard."""
        match self:
            case SDFStandard.OVI_3_0:
                return "3.0"
            case SDFStandard.IEEE_1497:
                return "4.0"

    @property
    def other(self) -> "SDFStandard":
        """Return the standard this one is not."""
        match self:
            case SDFStandard.OVI_3_0:
                return SDFStandard.IEEE_1497
            case SDFStandard.IEEE_1497:
                return SDFStandard.OVI_3_0

    @classmethod
    def declared_by(cls, sdfversion: str) -> "SDFStandard | None":
        """Return the standard an SDFVERSION string names, or None for older ones.

        P1497 Draft 0.10 reads the version from the first substring of the
        string that matches one of its listed versions, so ``"IEEE 1497 4.0"``
        names IEEE 1497 and ``"2.1"`` names neither standard.
        """
        found = [
            (index, version)
            for version in _VERSION_STRINGS
            if (index := sdfversion.find(version)) >= 0
        ]
        if not found:
            return None
        _, first = min(found)
        return next((s for s in cls if s.version == first), None)


@dataclass(frozen=True, kw_only=True)
class ParseOptions:
    """The rules one parse holds the text to.

    *standard* None applies the IEEE 1497 rules and skips the SDFVERSION
    check; an explicit standard also requires the SDFVERSION not to name the
    other one.  *loose_cond* lets a delay ``COND`` or ``CONDELSE`` wrap any
    number of entries of any delay type, which neither standard allows.
    """

    standard: SDFStandard | None = None
    loose_cond: bool = False

    @property
    def rules(self) -> SDFStandard:
        """Return the standard whose construct rules this parse applies."""
        return SDFStandard.IEEE_1497 if self.standard is None else self.standard
