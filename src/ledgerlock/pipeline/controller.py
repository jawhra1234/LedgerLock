"""The controller: runs the tiers in order and assembles one result.

Tiers are ordered by how much they can be trusted, not by how clever they are.
T1 settles everything it can prove; T2 only sees what T1 could not resolve; T3
only sees what T2 could not resolve. Each tier's residue is the next tier's
input, so the expensive machinery is never spent on records already closed.
"""

from __future__ import annotations

from . import tier1
from .result import Finding, ProposedLink, ReconResult, Tier
from .views import Index


def reconcile(ix: Index) -> ReconResult:
    links: list[ProposedLink] = []
    findings: list[Finding] = []
    tiers: list[Tier] = []

    l, f = tier1.run(ix)
    links += l
    findings += f
    tiers.append(Tier.T1)

    # T2 (rules, tolerances, subset-sum) and T3 (model-assisted residue) plug
    # in here, each receiving only what the previous tier left unresolved.

    return ReconResult(
        links=links,
        findings=findings,
        tiers_run=tiers,
        n_orders=len(ix.sources.orders),
        n_entries=len(ix.sources.entries),
        n_bank_lines=len(ix.sources.bank_lines),
        n_settlements=len(ix.settlements),
    )


def reconcile_sources(sources) -> ReconResult:
    return reconcile(Index.build(sources))
