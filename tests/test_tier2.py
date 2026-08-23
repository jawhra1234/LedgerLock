"""Tests for tier 2 and the subset-sum search.

The subset-sum tests come first and use hand-written integers, because that is
the one component in this project capable of inventing a plausible wrong answer
and it should be provable without any dataset at all.
"""

from dataclasses import replace

import pytest

from ledgerlock import config
from ledgerlock.domain.taxonomy import (
    EXCEPTION_META, ExceptionCode as EC, Resolvability,
)
from ledgerlock.eval.metrics import score
from ledgerlock.generate.engine import build
from ledgerlock.generate.params import PROFILES
from ledgerlock.generate.writer import write_world
from ledgerlock.io.loaders import load_sources, load_truth
from ledgerlock.pipeline import subsetsum
from ledgerlock.pipeline.controller import reconcile_sources
from ledgerlock.pipeline.result import Action, Tier


# ---------------------------------------------------------------------------
# the search, on numbers alone
# ---------------------------------------------------------------------------

def _find(target, cands, max_subset=3, max_candidates=12):
    return subsetsum.find_unique_subset(target, cands, max_subset, max_candidates)


def test_finds_a_single_candidate():
    r = _find(500, [("a", 500), ("b", 300)])
    assert r.subset == ("a",)
    assert not r.ambiguous


def test_finds_a_combination():
    r = _find(800, [("a", 500), ("b", 300), ("c", 111)])
    assert set(r.subset) == {"a", "b"}


def test_refuses_to_choose_between_two_valid_subsets():
    """The single most dangerous behaviour to get wrong. Two subsets both sum
    to 500, so the honest answer is 'I cannot tell', not a coin flip."""
    r = _find(500, [("a", 500), ("b", 200), ("c", 300)])
    assert r.subset is None
    assert r.ambiguous
    assert "refusing to choose" in r.why_not()


def test_admits_no_tolerance():
    """One paisa out is not a match. Tolerance belongs to a named rule with a
    named threshold, never buried inside a search."""
    assert _find(500, [("a", 499)]).subset is None
    assert _find(500, [("a", 501)]).subset is None


def test_respects_the_subset_size_cap():
    cands = [("a", 100), ("b", 100), ("c", 100), ("d", 100)]
    assert _find(400, cands, max_subset=3).subset is None
    assert _find(300, cands, max_subset=3).ambiguous     # many 3-subsets work


def test_truncation_is_reported_not_silent():
    cands = [(f"s{i}", 1000 + i) for i in range(30)]
    r = _find(999_999, cands, max_candidates=5)
    assert r.truncated
    assert r.considered == 5
    assert "truncated" in r.why_not()


def test_zero_target_and_empty_pool_find_nothing():
    assert _find(0, [("a", 100)]).subset is None
    assert _find(100, []).subset is None


def test_result_is_independent_of_input_order():
    a = _find(800, [("x", 500), ("y", 300)])
    b = _find(800, [("y", 300), ("x", 500)])
    assert set(a.subset) == set(b.subset)


# ---------------------------------------------------------------------------
# the pipeline, end to end
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    root = tmp_path_factory.mktemp("t2world")
    world = build(replace(PROFILES["default"], seed=42))
    manifest = write_world(world, root)
    src = load_sources(root / "raw")
    result = reconcile_sources(src, upto=Tier.T2)
    return score(result, load_truth(root / "truth"), src, manifest), result, src


# -- the claim, unchanged from T1 -------------------------------------------

def test_t2_asserts_no_false_matches(scored):
    """T2 is the first tier able to invent a link. It still must not."""
    s, _, _ = scored
    assert s.total_fp == 0


def test_t2_raises_no_false_alarms(scored):
    s, _, _ = scored
    assert s.false_alarms == [], [f.rule for f in s.false_alarms]


def test_t2_never_auto_resolves_an_unresolvable_case(scored):
    s, _, _ = scored
    assert s.unresolvable_auto_resolved == []


def test_settlement_bank_is_fully_recovered(scored):
    s, _, _ = scored
    assert s.links["settlement_bank"].recall == 1.0
    assert s.links["settlement_bank"].fp == 0


# -- what T2 must and must not close ---------------------------------------

CLOSED_BY_T2 = {
    EC.DUPLICATE_PAYMENT, EC.MISSING_IN_PG, EC.ORPHAN_PG_ENTRY,
    EC.ROUNDING_DRIFT, EC.MATERIAL_MISMATCH, EC.TIMING_UNSETTLED,
    EC.CROSS_CYCLE_REFUND, EC.UTR_CORRUPTED, EC.NON_PG_INFLOW,
    EC.MERGED_SETTLEMENT, EC.SPLIT_SETTLEMENT,
}
LEFT_FOR_T3 = {EC.UNEXPLAINED_ADJUSTMENT}


@pytest.mark.parametrize("code", sorted(CLOSED_BY_T2))
def test_codes_t2_classifies(scored, code):
    s, _, _ = scored
    cs = next(c for c in s.codes if c.code is code)
    assert cs.injected > 0
    assert cs.coded == cs.injected, f"{code}: {cs.coded}/{cs.injected} classified"


@pytest.mark.parametrize("code", sorted(LEFT_FOR_T3))
def test_codes_left_for_t3_are_reported_as_undetected(scored, code):
    """E12 has no order link and an opaque narration. Nothing deterministic
    can touch it, and the report says so rather than omitting it."""
    s, _, _ = scored
    cs = next(c for c in s.codes if c.code is code)
    assert cs.missed == cs.injected


def test_every_code_is_accounted_for(scored):
    assert (CLOSED_BY_T2 | LEFT_FOR_T3) == set(EC)


def test_no_unnamed_residue_remains(scored):
    s, _, _ = scored
    assert s.residue == [], [(f.rule, f.subject_id) for f in s.residue]


def test_classified_does_not_mean_resolved(scored):
    """The distinction the whole design rests on. E05 is classified perfectly
    and every one of them still goes to a human."""
    _, result, _ = scored
    material = result.findings_of(EC.MATERIAL_MISMATCH)
    assert material
    assert all(f.action is Action.ESCALATED for f in material)


def test_open_cases_are_exactly_the_ones_that_should_be(scored):
    """A pipeline reporting nothing open on this dataset has lied."""
    _, result, _ = scored
    open_codes = {f.code for f in result.escalated}
    assert EC.MATERIAL_MISMATCH in open_codes      # resolvability none
    assert EC.DUPLICATE_PAYMENT in open_codes      # resolvability partial
    assert EC.ROUNDING_DRIFT not in open_codes     # absorbed, within tolerance


# -- rule-level behaviour ---------------------------------------------------

def test_absorbed_drifts_stay_visible_with_their_amount(scored):
    """Absorbing a rounding drift silently is how small systematic overcharges
    live forever. Every absorbed case is still reported, with its size."""
    _, result, _ = scored
    drifts = result.findings_of(EC.ROUNDING_DRIFT)
    assert drifts
    assert all(f.action is Action.AUTO_RESOLVED for f in drifts)
    assert all(f.amount_delta not in (None, 0) for f in drifts)
    assert all(abs(f.amount_delta) <= config.ROUNDING_TOLERANCE_PAISE for f in drifts)


def test_gross_mismatches_are_never_absorbed(scored):
    """A fee is recomputed, so a small gap is arithmetic. A gross is asserted
    by the merchant, so any gap is two systems disagreeing about a sale."""
    _, result, _ = scored
    gross = [f for f in result.findings if f.rule == "gross_delta_vs_order"]
    assert gross
    assert all(f.action is Action.ESCALATED for f in gross)


def test_non_gateway_inflows_are_out_of_scope_not_unmatched(scored):
    _, result, _ = scored
    inflows = result.findings_of(EC.NON_PG_INFLOW)
    assert inflows
    assert all(f.action is Action.OUT_OF_SCOPE for f in inflows)


def test_recovered_links_carry_their_evidence_and_confidence(scored):
    _, result, _ = scored
    t2_links = [l for l in result.links if l.tier is Tier.T2]
    assert t2_links
    for l in t2_links:
        assert l.rule in ("amount_date_unique", "subset_sum_exact")
        assert l.evidence
        assert 0.9 <= l.confidence < 1.0     # never claims T1's certainty


def test_merged_credit_links_every_settlement_it_covers(scored):
    _, result, src = scored
    merged = result.findings_of(EC.MERGED_SETTLEMENT)
    assert merged
    by_line = {b.line_id: b for b in src.bank_lines}
    for f in merged:
        sids = {l.settlement_id for l in result.links_of("settlement_bank")
                if l.line_id == f.subject_id}
        assert len(sids) >= 2, f"{f.subject_id} covers {sids}"
        assert by_line[f.subject_id].credit > 0


def test_batch_gaps_are_explained_not_left_open(scored):
    _, result, _ = scored
    explained = [f for f in result.findings if f.rule == "batch_gap_attributed"]
    assert explained
    assert all(f.action is Action.EXPLAINED for f in explained)


def test_one_cause_is_reported_once(scored):
    """The collapse step: a T2 finding that names a subject replaces T1's
    unnamed flag on it, and one that explains another record clears that
    record's flag too."""
    _, result, _ = scored
    unnamed = {f.subject() for f in result.findings if f.code is None}
    named = {f.subject() for f in result.findings if f.code is not None}
    assert not (unnamed & named), "a subject is both named and unnamed"
    # And nothing a T2 finding explained on another record is still flagged.
    superseded = {k for f in result.findings for k in f.supersedes}
    still_flagged = {f.subject_key() for f in result.findings if f.code is None}
    assert not (superseded & still_flagged)


def test_auto_resolved_is_always_a_fully_resolvable_code(scored):
    _, result, _ = scored
    for f in result.findings:
        if f.action is Action.AUTO_RESOLVED:
            assert f.code is not None
            assert EXCEPTION_META[f.code].resolvability is Resolvability.FULL


# -- the arc, and scale -----------------------------------------------------

def test_t2_strictly_improves_on_t1(tmp_path):
    """The point of `upto`: the contribution of each tier is measured on one
    dataset, not asserted."""
    world = build(replace(PROFILES["default"], seed=42))
    manifest = write_world(world, tmp_path)
    src = load_sources(tmp_path / "raw")
    truth = load_truth(tmp_path / "truth")

    t1 = score(reconcile_sources(src, upto=Tier.T1), truth, src, manifest)
    t2 = score(reconcile_sources(src, upto=Tier.T2), truth, src, manifest)

    assert t2.links["settlement_bank"].recall > t1.links["settlement_bank"].recall
    assert t2.exceptions_coded > t1.exceptions_coded
    assert len(t2.residue) < len(t1.residue)
    assert t2.total_fp == t1.total_fp == 0        # improvement costs nothing


@pytest.mark.parametrize("profile", ["smoke", "default", "scale"])
def test_holds_at_every_scale(tmp_path_factory, profile):
    root = tmp_path_factory.mktemp(f"scale_{profile}")
    world = build(replace(PROFILES[profile], seed=42))
    manifest = write_world(world, root)
    src = load_sources(root / "raw")
    s = score(reconcile_sources(src, upto=Tier.T2),
              load_truth(root / "truth"), src, manifest)
    assert s.total_fp == 0
    assert s.false_alarms == []
    assert s.unresolvable_auto_resolved == []
    assert s.links["settlement_bank"].recall == 1.0
