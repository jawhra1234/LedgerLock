"""What the pipeline emits.

Every link carries the rule that produced it, the tier it came from, a
confidence and the evidence behind it. Nothing is ever posted anonymously --
if the pipeline claims two records belong together, it has to say why, and a
human has to be able to check it in one line.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from ..domain.taxonomy import ExceptionCode


class Tier(StrEnum):
    T1 = "t1"
    T2 = "t2"
    T3 = "t3"


class Action(StrEnum):
    """What the controller decided to DO about a finding.

    The distinction that matters is AUTO_RESOLVED versus ESCALATED. Anything
    auto-resolved is a claim the pipeline is confident enough to post without
    review, so eval holds it to a much harsher standard.
    """
    AUTO_RESOLVED = "auto_resolved"   # a named exception, closed without review
    ESCALATED = "escalated"           # a human must decide
    DEFERRED = "deferred"             # not an error; revisit next cycle
    OUT_OF_SCOPE = "out_of_scope"     # real, but not gateway money
    EXPLAINED = "explained"           # no action: fully accounted for by other
                                      # findings already raised


class ProposedLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    link_type: str
    order_id: str | None = None
    entry_id: str | None = None
    settlement_id: str | None = None
    line_id: str | None = None
    rule: str
    tier: Tier
    confidence: float = 1.0
    evidence: str = ""

    def key(self) -> tuple:
        """The tuple scored against ground truth."""
        return (self.link_type, self.order_id, self.entry_id,
                self.settlement_id, self.line_id)


class Finding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject_type: str                    # order | entry | bank_line | settlement
    subject_id: str
    action: Action
    rule: str
    tier: Tier
    # None means "something is wrong here and I cannot yet name it". Reporting
    # that honestly is the point; a tier that guesses a code to look complete
    # is worse than one that admits the gap.
    code: ExceptionCode | None = None
    confidence: float = 1.0
    detail: str = ""
    amount_delta: int | None = None      # paise, where a break has a size
    # Subject keys ("settlement:STL_0007") this finding explains on another
    # record, so the controller can drop the earlier unnamed flag instead of
    # reporting one cause twice.
    supersedes: tuple[str, ...] = ()

    def subject_key(self) -> str:
        return f"{self.subject_type}:{self.subject_id}"

    def subject(self) -> tuple[str, str]:
        return (self.subject_type, self.subject_id)


class ReconResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    links: list[ProposedLink] = []
    findings: list[Finding] = []
    tiers_run: list[Tier] = []
    n_orders: int = 0
    n_entries: int = 0
    n_bank_lines: int = 0
    n_settlements: int = 0

    def links_of(self, link_type: str) -> list[ProposedLink]:
        return [l for l in self.links if l.link_type == link_type]

    def findings_of(self, code: ExceptionCode | None) -> list[Finding]:
        return [f for f in self.findings if f.code is code]

    @property
    def unclassified(self) -> list[Finding]:
        """The honest residue: flagged, unnamed, and still open.

        An EXPLAINED finding is unnamed but not open -- the gap is accounted
        for by other findings -- so it is not residue.
        """
        return [f for f in self.findings
                if f.code is None and f.action is Action.ESCALATED]

    @property
    def escalated(self) -> list[Finding]:
        return [f for f in self.findings if f.action is Action.ESCALATED]
