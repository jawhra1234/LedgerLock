"""Bounded, exact, ambiguity-refusing subset sum.

Kept as a pure function over integers with no domain types, so it can be
tested on hand-written numbers without generating a dataset.

The design is defensive on purpose. Subset sum given a free hand is a machine
for producing plausible answers: with enough candidates, *some* subset sums to
almost any target, and in reconciliation a plausible wrong answer is the most
expensive output there is. So:

  * exact equality only -- no tolerance is admitted inside the search
  * the candidate pool and the subset size are both capped
  * if two different subsets both hit the target, it returns nothing and says
    the case is ambiguous, rather than picking one
  * truncation is reported, never silent
"""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from typing import Sequence

Candidate = tuple[str, int]      # (id, amount in paise)


@dataclass(frozen=True)
class SubsetResult:
    subset: tuple[str, ...] | None = None
    ambiguous: bool = False
    truncated: bool = False
    considered: int = 0
    max_subset: int = 0

    @property
    def found(self) -> bool:
        return self.subset is not None

    def why_not(self) -> str:
        if self.found:
            return ""
        if self.ambiguous:
            return (f"more than one subset of {self.considered} candidates sums "
                    "to the gap; refusing to choose")
        base = (f"no subset of up to {self.max_subset} of {self.considered} "
                "candidates sums to the gap")
        return base + " (candidate pool was truncated)" if self.truncated else base


def find_unique_subset(
    target: int,
    candidates: Sequence[Candidate],
    max_subset: int,
    max_candidates: int,
) -> SubsetResult:
    """Find the one subset of `candidates` summing exactly to `target`.

    Returns a result with `subset` set only when exactly one subset works.
    """
    if target == 0 or not candidates:
        return SubsetResult(considered=len(candidates), max_subset=max_subset)

    # Deterministic order before truncating, so the same inputs always yield
    # the same outcome regardless of how the caller happened to build the list.
    ordered = sorted(candidates, key=lambda c: (c[1], c[0]))
    truncated = len(ordered) > max_candidates
    pool = ordered[:max_candidates]

    hits: list[tuple[str, ...]] = []
    for size in range(1, min(max_subset, len(pool)) + 1):
        for combo in combinations(pool, size):
            if sum(amount for _, amount in combo) == target:
                hits.append(tuple(cid for cid, _ in combo))
                if len(hits) > 1:
                    # Two answers is already too many; stop looking.
                    return SubsetResult(ambiguous=True, truncated=truncated,
                                        considered=len(pool), max_subset=max_subset)
    return SubsetResult(
        subset=hits[0] if hits else None,
        truncated=truncated,
        considered=len(pool),
        max_subset=max_subset,
    )
