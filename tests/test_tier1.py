"""Tests for tier 1 and the scoring harness.

The two that matter most are test_t1_asserts_no_false_matches and
test_t1_raises_no_false_alarms. Everything else in this project is negotiable;
those two are the claim.
"""

from dataclasses import replace

import pytest

from ledgerlock.domain.taxonomy import (
    EXCEPTION_META, ExceptionCode as EC, Resolvability,
)
from ledgerlock.eval.metrics import score
from ledgerlock.eval.report import to_markdown
from ledgerlock.generate.engine import build
from ledgerlock.generate.params import PROFILES
from ledgerlock.generate.writer import write_world
from ledgerlock.io.loaders import load_sources, load_truth
from ledgerlock.pipeline.controller import reconcile_sources
from ledgerlock.pipeline.result import Action, ReconResult, Tier
from ledgerlock.pipeline.views import Index


@pytest.fixture(scope="module")
def scored(tmp_path_factory):
    """Generate, reconcile with T1 ONLY, and score, end to end.

    Pinned to `upto=Tier.T1` on purpose. Every assertion in this file is a
    claim about the deterministic tier in isolation, and the published T1
    baseline has to stay provable for the rest of the project's life -- if this
    fixture silently picked up later tiers, the arc would stop being evidence.
    """
    root = tmp_path_factory.mktemp("world")
    world = build(replace(PROFILES["default"], seed=42))
    manifest = write_world(world, root)
    src = load_sources(root / "raw")
    result = reconcile_sources(src, upto=Tier.T1)
    return score(result, load_truth(root / "truth"), src, manifest), result, src


# ---------------------------------------------------------------------------
# the claim
# ---------------------------------------------------------------------------

def test_t1_asserts_no_false_matches(scored):
    """A wrong link is worse than a missing one: a gap gets investigated, a
    false match gets posted. T1 uses no thresholds, so it must be exact."""
    s, _, _ = scored
    assert s.total_fp == 0, [ls for ls in s.links.values() if ls.fp]


def test_t1_raises_no_false_alarms(scored):
    """No flag on a record ground truth considers clean and unrelated."""
    s, _, _ = scored
    assert s.false_alarms == [], [f.rule for f in s.false_alarms]


def test_t1_never_auto_resolves_an_unresolvable_case(scored):
    s, _, _ = scored
    assert s.unresolvable_auto_resolved == []


def test_auto_resolved_findings_are_only_ever_fully_resolvable(scored):
    """Anything posted without review must be a case the taxonomy agrees is
    fully resolvable -- never a partial or an honest dead end."""
    _, result, _ = scored
    for f in result.findings:
        if f.action is Action.AUTO_RESOLVED:
            assert f.code is not None, f
            assert EXCEPTION_META[f.code].resolvability is Resolvability.FULL, f


# ---------------------------------------------------------------------------
# what T1 is and is not expected to solve
# ---------------------------------------------------------------------------

CLASSIFIED_AT_T1 = {
    EC.MISSING_IN_PG, EC.ORPHAN_PG_ENTRY, EC.DUPLICATE_PAYMENT,
    EC.TIMING_UNSETTLED, EC.SPLIT_SETTLEMENT,
}
# Detected but deliberately unnamed: naming these needs a tolerance, a search
# or a narration, none of which belong in a threshold-free tier.
DETECTED_UNNAMED_AT_T1 = {
    EC.ROUNDING_DRIFT, EC.MATERIAL_MISMATCH, EC.UTR_CORRUPTED,
    EC.NON_PG_INFLOW, EC.MERGED_SETTLEMENT,
}
# Genuinely invisible to T1, and recorded as such rather than papered over.
INVISIBLE_TO_T1 = {EC.CROSS_CYCLE_REFUND, EC.UNEXPLAINED_ADJUSTMENT}


@pytest.mark.parametrize("code", sorted(CLASSIFIED_AT_T1))
def test_codes_t1_fully_classifies(scored, code):
    s, _, _ = scored
    cs = next(c for c in s.codes if c.code is code)
    assert cs.injected > 0
    assert cs.coded == cs.injected, f"{code}: only {cs.coded}/{cs.injected} classified"


@pytest.mark.parametrize("code", sorted(DETECTED_UNNAMED_AT_T1))
def test_codes_t1_detects_but_declines_to_name(scored, code):
    s, _, _ = scored
    cs = next(c for c in s.codes if c.code is code)
    assert cs.injected > 0
    assert cs.detected == cs.injected, f"{code}: {cs.missed} slipped through"
    assert cs.miscoded == 0, f"{code}: guessed a wrong code {cs.miscoded} times"


@pytest.mark.parametrize("code", sorted(INVISIBLE_TO_T1))
def test_codes_t1_cannot_see_are_reported_as_missed(scored, code):
    """These are T2/T3 work. The point of the assertion is that the harness
    reports them as undetected rather than quietly omitting them."""
    s, _, _ = scored
    cs = next(c for c in s.codes if c.code is code)
    assert cs.injected > 0
    assert cs.missed == cs.injected


def test_every_code_is_accounted_for_in_this_test_file():
    """If a thirteenth code is added, this fails until its expectation is
    written down -- so no code can be added and quietly left unscored."""
    covered = CLASSIFIED_AT_T1 | DETECTED_UNNAMED_AT_T1 | INVISIBLE_TO_T1
    assert covered == set(EC)


# ---------------------------------------------------------------------------
# rule-level behaviour
# ---------------------------------------------------------------------------

def test_order_entry_is_perfect_and_orphans_are_refused(scored):
    """The claimed order_id column is verified, not trusted."""
    s, result, src = scored
    assert s.links["order_entry"].recall == 1.0
    assert s.links["order_entry"].precision == 1.0
    order_ids = {o.order_id for o in src.orders}
    for l in result.links_of("order_entry"):
        assert l.order_id in order_ids


def test_failed_orders_are_never_flagged(scored):
    _, result, src = scored
    failed = {o.order_id for o in src.orders if o.status.value == "failed"}
    assert failed
    flagged = {f.subject_id for f in result.findings if f.subject_type == "order"}
    assert not (failed & flagged)


def test_duplicates_are_escalated_never_resolved(scored):
    _, result, _ = scored
    dups = result.findings_of(EC.DUPLICATE_PAYMENT)
    assert dups
    assert all(f.action is Action.ESCALATED for f in dups)


def test_unsettled_entries_are_deferred_not_escalated(scored):
    """Nobody should be paged because a T+2 cycle has not run."""
    _, result, _ = scored
    unsettled = result.findings_of(EC.TIMING_UNSETTLED)
    assert unsettled
    assert all(f.action is Action.DEFERRED for f in unsettled)


def test_every_link_carries_its_rule_and_evidence(scored):
    _, result, _ = scored
    assert result.links
    for l in result.links:
        assert l.rule and l.evidence
        assert l.tier is Tier.T1
        assert l.confidence == 1.0      # T1 is exact or it is not T1


def test_amount_breaks_record_their_size(scored):
    """An unnamed break still has to say how big it is, or a human cannot
    triage it."""
    _, result, _ = scored
    sized = [f for f in result.findings
             if f.rule in ("fee_recompute", "gross_vs_order_amount")]
    assert sized
    assert all(f.amount_delta not in (None, 0) for f in sized)


def test_split_settlement_is_proven_not_guessed(scored):
    """The only auto-resolution T1 permits itself: several credits carrying one
    UTR and summing to the payout exactly."""
    _, result, src = scored
    splits = result.findings_of(EC.SPLIT_SETTLEMENT)
    assert splits
    by_line = {b.line_id: b for b in src.bank_lines}
    ix = Index.build(src)
    for f in splits:
        lines = [l.line_id for l in result.links_of("settlement_bank")
                 if l.settlement_id == f.subject_id]
        assert len(lines) >= 2
        assert sum(by_line[l].credit for l in lines) == ix.settlements[f.subject_id].payout


# ---------------------------------------------------------------------------
# harness plumbing
# ---------------------------------------------------------------------------

def test_result_survives_a_json_round_trip(scored):
    _, result, _ = scored
    again = ReconResult.model_validate_json(result.model_dump_json())
    assert again == result


def test_entry_settlement_is_excluded_from_scoring(scored):
    """Scoring a column the source hands over would inflate the match rate."""
    s, _, _ = scored
    assert "entry_settlement" not in s.links


def test_markdown_report_states_the_seed_and_both_headlines(scored):
    s, _, _ = scored
    md = to_markdown(s)
    assert "seed **42**" in md
    assert "settlement -> bank matching" in md
    assert "false-match rate" in md
    assert "python -m ledgerlock generate" in md


def test_smoke_profile_also_reconciles_without_false_matches(tmp_path):
    """The 50-order bar, so the claim is not an artefact of one dataset size."""
    world = build(replace(PROFILES["smoke"], seed=7))
    manifest = write_world(world, tmp_path)
    src = load_sources(tmp_path / "raw")
    s = score(reconcile_sources(src, upto=Tier.T1),
              load_truth(tmp_path / "truth"), src, manifest)
    assert s.total_fp == 0
    assert s.false_alarms == []
