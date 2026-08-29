"""Tests for the robustness sweep.

The sweep exists to answer "is seed 42 lucky?", so the first thing to prove is
that it could ever say no. A sweep that reports 100% because it is measuring
nothing is worse than no sweep: it manufactures confidence.
"""

import statistics

import pytest

from ledgerlock.pipeline.result import Tier
from ledgerlock.sweep import SweepResult, SweepRow, run_sweep, to_markdown

SEEDS = [1, 2, 3]


@pytest.fixture(scope="module")
def t2():
    return run_sweep(["smoke"], SEEDS, Tier.T2)


@pytest.fixture(scope="module")
def t1():
    return run_sweep(["smoke"], SEEDS, Tier.T1)


def test_the_sweep_measures_something(t1, t2):
    """The load-bearing test in this file.

    T1 and T2 run over the identical worlds, so if the sweep cannot tell them
    apart it is not reading the pipeline at all. T1 leaves mangled UTRs and
    merged credits unrecovered; T2 recovers them.
    """
    r1 = [r.recall for r in t1.rows]
    r2 = [r.recall for r in t2.rows]
    assert statistics.mean(r1) < statistics.mean(r2), (
        f"T1 mean {statistics.mean(r1):.3f} is not below T2 {statistics.mean(r2):.3f}")
    assert min(r1) < 1.0, "T1 recovered everything, which cannot be right"


def test_recall_is_computed_from_real_link_counts(t2):
    """Guards the trap in `recall`: an empty truth set returns 1.0, so a sweep
    over worlds with no links would proudly report a perfect score."""
    assert t2.rows
    for r in t2.rows:
        assert r.sb_truth > 0, "no settlement->bank links to match"
        assert r.injected > 0, "no exceptions injected"
        assert r.records > 0


def test_each_seed_produces_a_different_world(t2):
    """Otherwise the sweep is one dataset counted three times."""
    shapes = {(r.records, r.injected, r.sb_truth) for r in t2.rows}
    assert len(shapes) > 1


def test_no_world_produces_a_false_match(t2):
    assert t2.false_matches == 0
    assert t2.wrongly_resolved == 0
    assert t2.dirty == []


def test_aggregates_add_up(t2):
    assert t2.datasets == len(SEEDS)
    assert t2.records == sum(r.records for r in t2.rows)
    assert t2.injected == sum(r.injected for r in t2.rows)


def test_a_broken_world_is_flagged_not_averaged_away():
    """One bad world in a hundred must surface, not vanish into a median."""
    good = SweepRow("smoke", 1, 100, 10, 9, 9, 0, 0, 0, 9, 0)
    bad = SweepRow("smoke", 2, 100, 10, 9, 9, 1, 0, 0, 9, 0)
    res = SweepResult(rows=[good] * 99 + [bad])
    assert good.clean and not bad.clean
    assert res.false_matches == 1
    assert res.dirty == [bad]


def test_wrongly_resolving_an_unresolvable_case_is_also_dirty():
    row = SweepRow("smoke", 1, 100, 10, 9, 9, 0, 0, 1, 9, 0)
    assert not row.clean
    assert SweepResult(rows=[row]).dirty == [row]


def test_worst_seed_is_reported_so_it_can_be_reproduced(t2):
    worst = t2.worst("smoke")
    assert worst is not None
    assert worst.seed in SEEDS
    assert worst.recall == min(r.recall for r in t2.rows)


def test_markdown_states_the_scope_and_the_exclusion(t2):
    md = to_markdown(t2)
    assert "independently generated worlds" in md
    assert "false matches" in md
    # The T3 exclusion is a claim about method and must be stated, not implied.
    assert "T3 is excluded" in md
    assert "| `smoke` | 1 |" in md      # every world listed, not just a summary


def test_the_sweep_catches_a_deliberately_broken_matcher(monkeypatch):
    """Mutation test: the sweep must be able to fail.

    Every other assertion here checks the sweep reports clean on a correct
    pipeline, which a sweep that measures nothing would also do. This breaks
    R11's uniqueness requirement -- turning it into exactly the confident wrong
    match this project exists to avoid -- and asserts the sweep says so.
    """
    from ledgerlock.pipeline import tier2
    from ledgerlock.pipeline.result import ProposedLink

    def sloppy(ix, links):
        """Link every unmatched settlement to the first unclaimed credit,
        amount be damned."""
        claimed = {l.line_id for l in links if l.link_type == "settlement_bank"}
        free = [b for b in ix.credit_lines if b.line_id not in claimed]
        out = []
        for st in tier2._unmatched_settlements(ix, links):
            if not free:
                break
            b = free.pop(0)
            out.append(ProposedLink(
                link_type="settlement_bank", settlement_id=st.settlement_id,
                line_id=b.line_id, rule="sabotage", tier=Tier.T2,
                confidence=0.95, evidence="deliberately wrong"))
        return out, []

    monkeypatch.setattr(tier2, "r11_recover_by_amount_and_date", sloppy)
    broken = run_sweep(["smoke"], SEEDS, Tier.T2)

    assert broken.false_matches > 0, "a sabotaged matcher swept clean"
    assert broken.dirty, "no world was flagged despite wrong links"
    # And it names the worlds, so a real regression is reproducible.
    assert all(r.seed in SEEDS for r in broken.dirty)
