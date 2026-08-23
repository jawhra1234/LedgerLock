"""Scoring the pipeline against ground truth.

Three deliberate choices, each of which lowers the headline number:

  1. `entry_settlement` links are NOT scored. The gateway report hands that
     column over, so "matching" it is a column read, not a match. Counting
     those 500-odd links would inflate the match rate by a wide margin while
     proving nothing. The exclusion is reported, not hidden.

  2. A wrong link and a hallucinated link are counted separately from a missed
     one. In finance a false match is far more expensive than a gap: a gap gets
     investigated, a false match gets posted. False-match rate is therefore
     reported next to the match rate every time, never on its own page.

  3. A truth exception marked `resolvability = none` is scored on whether the
     pipeline correctly REFUSED to resolve it. Auto-resolving one is counted as
     a failure even though the records would appear to tie.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..domain.taxonomy import EXCEPTION_META, ExceptionCode, Resolvability
from ..io.loaders import Sources, Truth
from ..pipeline.result import Action, Finding, ReconResult

SCORED_LINK_TYPES = ("order_entry", "settlement_bank")
EXCLUDED_LINK_TYPES = {
    "entry_settlement":
        "handed over by the gateway report as a column; scoring it would "
        "inflate the match rate without demonstrating a match",
}

Subject = tuple[str, str]


@dataclass
class LinkScore:
    link_type: str
    proposed: int = 0
    in_truth: int = 0
    tp: int = 0
    fp: int = 0
    fn: int = 0

    @property
    def precision(self) -> float:
        return self.tp / self.proposed if self.proposed else 1.0

    @property
    def recall(self) -> float:
        return self.tp / self.in_truth if self.in_truth else 1.0

    @property
    def false_match_rate(self) -> float:
        """Share of what the pipeline asserted that was simply wrong."""
        return self.fp / self.proposed if self.proposed else 0.0


@dataclass
class CodeScore:
    code: ExceptionCode
    injected: int = 0
    detected: int = 0          # something was flagged on the subject
    coded: int = 0             # flagged with the right code
    miscoded: int = 0          # flagged with a confidently WRONG code
    unclassified: int = 0      # flagged, honestly unnamed
    missed: int = 0            # never flagged at all
    resolved_at: dict[str, int] = field(default_factory=dict)

    @property
    def detection_rate(self) -> float:
        return self.detected / self.injected if self.injected else 1.0

    @property
    def classification_rate(self) -> float:
        return self.coded / self.injected if self.injected else 1.0


@dataclass
class Score:
    links: dict[str, LinkScore]
    codes: list[CodeScore]
    false_alarms: list[Finding]
    corroborating: list[Finding]
    unresolvable_auto_resolved: list[Finding]
    residue: list[Finding]
    n_records: int
    tiers: list[str]
    manifest: dict = field(default_factory=dict)

    # -- headline ----------------------------------------------------------
    @property
    def total_tp(self) -> int:
        return sum(s.tp for s in self.links.values())

    @property
    def total_truth(self) -> int:
        return sum(s.in_truth for s in self.links.values())

    @property
    def total_proposed(self) -> int:
        return sum(s.proposed for s in self.links.values())

    @property
    def total_fp(self) -> int:
        return sum(s.fp for s in self.links.values())

    @property
    def match_rate(self) -> float:
        return self.total_tp / self.total_truth if self.total_truth else 1.0

    @property
    def false_match_rate(self) -> float:
        return self.total_fp / self.total_proposed if self.total_proposed else 0.0

    @property
    def exceptions_injected(self) -> int:
        return sum(c.injected for c in self.codes)

    @property
    def exceptions_detected(self) -> int:
        return sum(c.detected for c in self.codes)

    @property
    def exceptions_coded(self) -> int:
        return sum(c.coded for c in self.codes)

    @property
    def exceptions_missed(self) -> int:
        return sum(c.missed for c in self.codes)


def _related(truth: Truth) -> dict[Subject, set[Subject]]:
    """Subjects that share a true edge.

    Used to separate a *corroborating* flag from a false alarm. When the bank
    merges two payouts into one credit, the truth file blames the bank line --
    but the second settlement genuinely has no credit carrying its UTR, and
    reporting that is correct behaviour, not noise. It is only a false alarm if
    the flagged record has no true connection to anything actually broken.
    """
    rel: dict[Subject, set[Subject]] = {}

    def join(a: Subject, b: Subject) -> None:
        rel.setdefault(a, set()).add(b)
        rel.setdefault(b, set()).add(a)

    for l in truth.links:
        if l.order_id and l.entry_id:
            join(("order", l.order_id), ("entry", l.entry_id))
        if l.entry_id and l.settlement_id:
            join(("entry", l.entry_id), ("settlement", l.settlement_id))
        if l.settlement_id and l.line_id:
            join(("settlement", l.settlement_id), ("bank_line", l.line_id))
    return rel


def score(result: ReconResult, truth: Truth, sources: Sources,
          manifest: dict | None = None) -> Score:
    # ---- links -----------------------------------------------------------
    links: dict[str, LinkScore] = {}
    for lt in SCORED_LINK_TYPES:
        truth_keys = {
            (l.link_type, l.order_id, l.entry_id, l.settlement_id, l.line_id)
            for l in truth.links if l.link_type == lt
        }
        proposed_keys = {l.key() for l in result.links_of(lt)}
        s = LinkScore(link_type=lt, proposed=len(proposed_keys),
                      in_truth=len(truth_keys))
        s.tp = len(proposed_keys & truth_keys)
        s.fp = len(proposed_keys - truth_keys)
        s.fn = len(truth_keys - proposed_keys)
        links[lt] = s

    # ---- exceptions ------------------------------------------------------
    truth_by_subject: dict[Subject, list] = {}
    for x in truth.exceptions:
        truth_by_subject.setdefault((x.subject_type, x.subject_id), []).append(x)

    found_by_subject: dict[Subject, list[Finding]] = {}
    for f in result.findings:
        found_by_subject.setdefault(f.subject(), []).append(f)

    scores = {c: CodeScore(code=c) for c in ExceptionCode}
    for subject, xs in truth_by_subject.items():
        for x in xs:
            cs = scores[x.code]
            cs.injected += 1
            fs = found_by_subject.get(subject, [])
            if not fs:
                cs.missed += 1
                continue
            cs.detected += 1
            codes = {f.code for f in fs if f.code is not None}
            if x.code in codes:
                cs.coded += 1
                for f in fs:
                    if f.code is x.code:
                        cs.resolved_at[f.tier.value] = \
                            cs.resolved_at.get(f.tier.value, 0) + 1
            elif codes:
                cs.miscoded += 1
            else:
                cs.unclassified += 1

    # ---- flags on records ground truth considers clean --------------------
    rel = _related(truth)
    false_alarms: list[Finding] = []
    corroborating: list[Finding] = []
    for subject, fs in found_by_subject.items():
        if subject in truth_by_subject:
            continue
        neighbours = rel.get(subject, set())
        broken_neighbour = any(n in truth_by_subject for n in neighbours)
        (corroborating if broken_neighbour else false_alarms).extend(fs)

    # ---- the line that must stay at zero ---------------------------------
    never_resolvable = {
        (x.subject_type, x.subject_id) for x in truth.exceptions
        if EXCEPTION_META[x.code].resolvability is Resolvability.NONE
    }
    auto_on_unresolvable = [
        f for f in result.findings
        if f.action is Action.AUTO_RESOLVED and f.subject() in never_resolvable
    ]

    return Score(
        links=links,
        codes=[scores[c] for c in ExceptionCode],
        false_alarms=false_alarms,
        corroborating=corroborating,
        unresolvable_auto_resolved=auto_on_unresolvable,
        residue=result.unclassified,
        n_records=len(sources.orders) + len(sources.entries) + len(sources.bank_lines),
        tiers=[t.value for t in result.tiers_run],
        manifest=manifest or {},
    )
