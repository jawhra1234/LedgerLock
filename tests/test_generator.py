"""Tests for the generator and its ground truth.

The important ones are test_untouched_settlements_still_tie (proves injection is
surgical) and test_truth_references_are_intact (proves the ground truth is
usable as a scoring key at all). If either breaks, every accuracy number this
project would later publish is meaningless.
"""

from dataclasses import replace

import pytest

from ledgerlock.domain.models import EntryType
from ledgerlock.domain.taxonomy import ExceptionCode
from ledgerlock.generate.engine import build
from ledgerlock.generate.params import PROFILES
from ledgerlock.generate.writer import write_world
from ledgerlock.io.loaders import TruthLeak, load_sources, load_truth

AMOUNT_BREAKING = {ExceptionCode.ROUNDING_DRIFT, ExceptionCode.MATERIAL_MISMATCH}


@pytest.fixture(scope="module")
def smoke():
    return build(PROFILES["smoke"])


@pytest.fixture(scope="module")
def default():
    return build(PROFILES["default"])


def test_build_verifies_the_clean_world(smoke):
    # build() asserts the settlement identity before injection; reaching here
    # at all means the clean world balanced.
    assert smoke.settlements
    assert smoke.orders and smoke.entries and smoke.bank_lines


@pytest.mark.parametrize("profile", ["smoke", "default"])
def test_every_exception_code_is_exercised(profile):
    w = build(PROFILES[profile])
    seen = {x.code for x in w.exceptions}
    missing = set(ExceptionCode) - seen
    assert not missing, f"{profile} never exercises {sorted(c.value for c in missing)}"


def test_untouched_settlements_still_tie(default):
    """Injection must be surgical.

    A settlement whose members carry no amount-breaking exception must still
    satisfy sum(member nets) == total bank credit, even after the structural
    injectors have moved money and restructured bank lines around it.
    """
    broken_entries = {x.subject_id for x in default.exceptions
                      if x.code in AMOUNT_BREAKING}
    lines_for = {}
    for l in default.links:
        if l.link_type == "settlement_bank":
            lines_for.setdefault(l.settlement_id, []).append(l.line_id)
    by_id = {b.line_id: b for b in default.bank_lines}

    checked = 0
    for sid in default.settlements:
        members = default.entries_of(sid)
        if any(e.entry_id in broken_entries for e in members):
            continue
        credited = sum(by_id[lid].credit for lid in lines_for[sid]
                       if lid in by_id)
        # A merged line covers two settlements, so its credit exceeds this
        # settlement's payout; those are checked by test_merged_lines_tie.
        if len({s for s, lids in lines_for.items() if set(lids) & set(lines_for[sid])}) > 1:
            continue
        assert sum(e.net for e in members) == credited, sid
        checked += 1
    assert checked > 0


def test_merged_lines_tie_across_their_settlements(default):
    """E10 breaks a link, not an amount: the merged credit must still equal the
    sum of the payouts it covers."""
    broken = {x.subject_id for x in default.exceptions if x.code in AMOUNT_BREAKING}
    settlements_for = {}
    for l in default.links:
        if l.link_type == "settlement_bank":
            settlements_for.setdefault(l.line_id, set()).add(l.settlement_id)
    for line in default.bank_lines:
        sids = settlements_for.get(line.line_id, set())
        if len(sids) < 2:
            continue
        members = [e for sid in sids for e in default.entries_of(sid)]
        if any(e.entry_id in broken for e in members):
            continue
        assert sum(e.net for e in members) == line.credit, line.line_id


def test_split_lines_sum_back_to_one_payout(default):
    lines_for = {}
    for l in default.links:
        if l.link_type == "settlement_bank":
            lines_for.setdefault(l.settlement_id, []).append(l.line_id)
    by_id = {b.line_id: b for b in default.bank_lines}
    splits = [x for x in default.exceptions
              if x.code is ExceptionCode.SPLIT_SETTLEMENT]
    assert splits
    for x in splits:
        parts = [by_id[l] for l in lines_for[x.subject_id] if l in by_id]
        assert len(parts) >= 2
        assert sum(p.credit for p in parts) == default.settlements[x.subject_id].payout


def test_truth_references_are_intact(default):
    """Ground truth is only a scoring key if every id in it actually resolves."""
    order_ids = {o.order_id for o in default.orders}
    entry_ids = {e.entry_id for e in default.entries}
    line_ids = {b.line_id for b in default.bank_lines}
    settlement_ids = set(default.settlements)

    for l in default.links:
        if l.entry_id:
            assert l.entry_id in entry_ids, l
        if l.line_id:
            assert l.line_id in line_ids, l
        if l.settlement_id:
            assert l.settlement_id in settlement_ids, l

    pools = {"order": order_ids, "entry": entry_ids,
             "bank_line": line_ids, "settlement": settlement_ids}
    for x in default.exceptions:
        assert x.subject_id in pools[x.subject_type], x


def test_orphan_and_missing_are_genuinely_absent(default):
    """The two tier-1 codes must actually be true of the data, not just labelled."""
    order_ids = {o.order_id for o in default.orders}
    entry_orders = {e.order_id for e in default.entries if e.order_id}

    for x in default.exceptions:
        if x.code is ExceptionCode.ORPHAN_PG_ENTRY:
            e = default.entry(x.subject_id)
            assert e is not None and e.order_id not in order_ids
        if x.code is ExceptionCode.MISSING_IN_PG:
            assert x.subject_id in order_ids
            assert x.subject_id not in entry_orders


def test_no_record_carries_two_exceptions(default):
    """One subject, one injected failure -- otherwise the ground truth cannot
    say which failure a matcher was supposed to find."""
    seen = set()
    for x in default.exceptions:
        key = (x.subject_type, x.subject_id)
        assert key not in seen, f"{key} was corrupted twice"
        seen.add(key)


def test_unsettled_entries_have_no_settlement_reference(default):
    for x in default.exceptions:
        if x.code is ExceptionCode.TIMING_UNSETTLED:
            e = default.entry(x.subject_id)
            assert e is not None
            assert e.settlement_id is None and e.settled_at is None


def test_failed_orders_are_noise_not_exceptions(default):
    """An ERP order that failed has no gateway entry and must not be flagged --
    otherwise the pipeline learns to cry wolf on normal traffic."""
    flagged = {x.subject_id for x in default.exceptions}
    failed = [o for o in default.orders if o.status.value == "failed"]
    assert failed
    assert not [o for o in failed if o.order_id in flagged]


def test_settlement_rows_mirror_their_payout(default):
    """The gateway's aggregate row must never contradict its own members, even
    after injectors have moved money into or out of a settlement."""
    rows = {e.settlement_id: e for e in default.entries
            if e.entry_type is EntryType.SETTLEMENT}
    for sid, st in default.settlements.items():
        assert rows[sid].net == -st.payout, sid


# ---------------------------------------------------------------------------
# reproducibility and the truth-leak guard
# ---------------------------------------------------------------------------

def _write(tmp_path, seed, profile="smoke"):
    spec = replace(PROFILES[profile], seed=seed)
    write_world(build(spec), tmp_path)
    return {p.name: p.read_bytes()
            for p in (tmp_path / "raw").iterdir()}


def test_same_seed_is_byte_identical(tmp_path):
    a = _write(tmp_path / "a", 42)
    b = _write(tmp_path / "b", 42)
    assert a == b


def test_different_seed_is_a_different_world(tmp_path):
    a = _write(tmp_path / "a", 42)
    b = _write(tmp_path / "b", 43)
    assert a.keys() == b.keys()
    assert a != b


def test_pipeline_cannot_read_ground_truth(tmp_path):
    _write(tmp_path / "w", 42)
    with pytest.raises(TruthLeak):
        load_sources(tmp_path / "w" / "truth")


def test_csv_round_trip_preserves_every_field(tmp_path):
    spec = replace(PROFILES["smoke"], seed=42)
    world = build(spec)
    write_world(world, tmp_path)
    src = load_sources(tmp_path / "raw")
    truth = load_truth(tmp_path / "truth")

    assert len(src.orders) == len(world.orders)
    assert len(src.entries) == len(world.entries)
    assert len(src.bank_lines) == len(world.bank_lines)
    assert len(truth.exceptions) == len(world.exceptions)
    assert src.entries[0] == world.entries[0]
    # Blank cells must come back as None/0, never as the string "".
    assert all(e.order_id is None or e.order_id for e in src.entries)
    assert all(isinstance(e.net, int) for e in src.entries)
