"""The controller: runs the tiers in order and assembles one result.

Tiers are ordered by how much they can be trusted, not by how clever they are.
T1 settles everything it can prove; T2 only sees what T1 could not resolve; T3
only sees what T2 could not resolve. Each tier's residue is the next tier's
input, so the expensive machinery is never spent on records already closed.

`upto` exists so the contribution of each tier is measurable rather than
asserted -- `run --upto t1` reproduces the T1 baseline exactly, on the same
dataset, at any point in the project's life.
"""

from __future__ import annotations

from .. import config
from . import tier1, tier2, tier3
from ..llm.adapter import LLMClient, Mode
from .result import Action, Finding, ProposedLink, ReconResult, Tier
from .views import Index

ORDER = (Tier.T1, Tier.T2, Tier.T3)


def _residue(findings: list[Finding]) -> list[Finding]:
    """What a tier flagged but could not name -- the next tier's whole input."""
    return [f for f in findings
            if f.code is None and f.action is Action.ESCALATED]


def _collapse(findings: list[Finding]) -> list[Finding]:
    """Report one cause once.

    When a later tier names a subject, the earlier unnamed flag on it is
    dropped; and when a finding explains a *different* record (a bank line that
    accounts for an unmatched settlement), the flag on that record goes too.
    Without this the residue count stays inflated and every tier looks like it
    achieved less than it did.
    """
    named = {f.subject() for f in findings if f.code is not None}
    superseded = {key for f in findings for key in f.supersedes}
    return [
        f for f in findings
        if f.code is not None
        or (f.subject() not in named and f.subject_key() not in superseded)
    ]


def reconcile(ix: Index, upto: Tier = Tier.T2,
              llm: LLMClient | None = None) -> ReconResult:
    wanted = ORDER[:ORDER.index(upto) + 1]
    links: list[ProposedLink] = []
    findings: list[Finding] = []
    tiers: list[Tier] = []

    l, f = tier1.run(ix)
    links += l
    findings += f
    tiers.append(Tier.T1)

    if Tier.T2 in wanted:
        l, f = tier2.run(ix, links, _residue(findings))
        links += l
        findings += f
        tiers.append(Tier.T2)

    explanations: dict[str, str] = {}
    llm_summary: dict = {}
    if Tier.T3 in wanted:
        # T3 returns no links. A tier that cannot create a link cannot create a
        # false match, which is why that guarantee is structural here rather
        # than a property of the model behaving well today.
        client = llm or LLMClient(_default_provider(), config.LLM_CACHE_DIR,
                                  mode=Mode.CACHED)
        f, _ = tier3.run(ix, client, _residue(findings))
        findings += f
        explanations = tier3.r18_explain(ix, client, _collapse(findings))
        llm_summary = client.summary()
        tiers.append(Tier.T3)

    return ReconResult(
        links=links,
        findings=_collapse(findings),
        explanations=explanations,
        llm=llm_summary,
        tiers_run=tiers,
        n_orders=len(ix.sources.orders),
        n_entries=len(ix.sources.entries),
        n_bank_lines=len(ix.sources.bank_lines),
        n_settlements=len(ix.settlements),
    )


def _default_provider():
    from ..llm.gemini import OfflineProvider
    return OfflineProvider()


def reconcile_sources(sources, upto: Tier = Tier.T2,
                      llm: LLMClient | None = None) -> ReconResult:
    return reconcile(Index.build(sources), upto=upto, llm=llm)
