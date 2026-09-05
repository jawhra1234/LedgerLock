"""Tests for the dashboard's data layer.

The dashboard is a *view*. The single most important property is that it shows
the numbers the harness measured rather than numbers of its own -- so the tests
here mostly check that it reads, filters and groups without ever computing a
metric. A viewer with its own reconciliation logic would be a second
implementation free to disagree with the one that was actually scored.

The Streamlit app itself is a thin shell over these functions, which is why
they live in a module a test can import without a browser.
"""

import json
from pathlib import Path

import pytest

from ledgerlock.dashboard import (
    ACTION_ORDER, Board, MissingArtifacts, fmt_paise, load_board, load_sweep,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src" / "ledgerlock"


@pytest.fixture(scope="module")
def board(tmp_path_factory):
    """Build real artefacts through the CLI's own code path, then read them."""
    from dataclasses import replace

    from ledgerlock import config
    from ledgerlock.eval.metrics import score, score_to_dict
    from ledgerlock.generate.engine import build
    from ledgerlock.generate.params import PROFILES
    from ledgerlock.generate.writer import write_world
    from ledgerlock.io.loaders import load_sources, load_truth
    from ledgerlock.llm.adapter import LLMClient, Mode
    from ledgerlock.llm.gemini import OfflineProvider
    from ledgerlock.pipeline.controller import reconcile_sources
    from ledgerlock.pipeline.result import Tier

    root = tmp_path_factory.mktemp("board")
    manifest = write_world(build(replace(PROFILES["smoke"], seed=42)), root)
    src = load_sources(root / "raw")
    client = LLMClient(OfflineProvider(), config.LLM_CACHE_DIR, mode=Mode.CACHED)
    result = reconcile_sources(src, upto=Tier.T3, llm=client)
    s = score(result, load_truth(root / "truth"), src, manifest)

    out = root / "out"
    out.mkdir()
    (out / "recon.json").write_text(result.model_dump_json(indent=2),
                                    encoding="utf-8")
    (out / "score.json").write_text(json.dumps(score_to_dict(s), default=str),
                                    encoding="utf-8")
    return load_board(out)


# ---------------------------------------------------------------------------
# it reads rather than computes
# ---------------------------------------------------------------------------

def test_the_dashboard_module_contains_no_reconciliation_logic():
    """Structural guard. A viewer that imported the pipeline could recompute a
    match, and the screen would stop being the measured result."""
    source = (SRC / "dashboard.py").read_text(encoding="utf-8")
    for forbidden in ("tier1", "tier2", "tier3", "reconcile", "subsetsum",
                      "load_truth", "score("):
        assert forbidden not in source, f"dashboard imports/uses {forbidden!r}"


def test_headline_numbers_come_straight_from_score_json(board):
    assert board.link("settlement_bank")["recall"] == \
        board.score["links"]["settlement_bank"]["recall"]
    assert board.totals["false_matches"] == board.score["totals"]["false_matches"]
    assert board.records == board.score["n_records"]


def test_it_reports_the_profile_seed_and_tiers(board):
    assert board.profile == "smoke"
    assert board.seed == 42
    assert board.tiers == "t1+t2+t3"
    assert "generate" in board.reproduce


# ---------------------------------------------------------------------------
# grouping and filtering
# ---------------------------------------------------------------------------

def test_findings_group_by_action_in_reviewer_order(board):
    grouped = board.by_action()
    assert grouped
    assert "escalated" in grouped
    order = [a for a in ACTION_ORDER if a in grouped]
    assert list(grouped)[:len(order)] == order   # decisions first, noise last
    assert sum(len(v) for v in grouped.values()) == len(board.findings)


def test_filtering_narrows_and_never_invents(board):
    everything = board.filtered()
    assert len(everything) == len(board.findings)

    escalated = board.filtered(actions=["escalated"])
    assert escalated
    assert all(f["action"] == "escalated" for f in escalated)
    assert len(escalated) <= len(everything)

    code = board.codes_present()[0]
    by_code = board.filtered(codes=[code])
    assert by_code and all(f["code"] == code for f in by_code)


def test_findings_are_sorted_by_size_so_triage_starts_at_the_top(board):
    rows = board.filtered(actions=["escalated"])
    amounts = [abs(f.get("amount_delta") or 0) for f in rows]
    assert amounts == sorted(amounts, reverse=True)


def test_search_matches_id_rule_and_evidence(board):
    a_finding = board.filtered()[0]
    assert board.filtered(query=a_finding["subject_id"])
    assert board.filtered(query=a_finding["rule"])
    assert board.filtered(query="a string that appears nowhere at all") == []


# ---------------------------------------------------------------------------
# the audit trail
# ---------------------------------------------------------------------------

def test_audit_returns_every_link_and_finding_for_one_record(board):
    subject = board.filtered(actions=["escalated"])[0]["subject_id"]
    a = board.audit(subject)
    assert a["subject_id"] == subject
    assert a["findings"]
    assert all(f["subject_id"] == subject for f in a["findings"])
    for f in a["findings"]:
        assert f["rule"] and f["tier"]          # never anonymous
        assert f["detail"]


def test_audit_finds_a_settlement_through_its_links(board):
    sid = next(l["settlement_id"] for l in board.links
               if l.get("settlement_id"))
    a = board.audit(sid)
    assert a["links"], "a settlement with links returned none"
    assert all(l["evidence"] for l in a["links"])


def test_audit_of_an_unknown_id_is_empty_not_an_error(board):
    a = board.audit("ORD_DOES_NOT_EXIST")
    assert a["links"] == [] and a["findings"] == []
    assert board.audit("")["subject_id"] == ""


# ---------------------------------------------------------------------------
# tier attribution
# ---------------------------------------------------------------------------

def test_tier_attribution_is_read_not_recomputed(board):
    work = board.work_by_tier()
    assert work
    assert sum(v["links"] for v in work.values()) == len(board.links)
    assert sum(v["findings"] for v in work.values()) == len(board.findings)


def test_the_model_tier_asserts_no_links(board):
    """The structural guarantee, visible in the view as well as the tests."""
    work = board.work_by_tier()
    assert work.get("t3", {}).get("links", 0) == 0


# ---------------------------------------------------------------------------
# failure modes
# ---------------------------------------------------------------------------

def test_missing_artifacts_explain_what_to_run(tmp_path):
    with pytest.raises(MissingArtifacts) as e:
        load_board(tmp_path)
    msg = str(e.value)
    assert "recon.json" in msg and "score.json" in msg
    assert "python -m ledgerlock run" in msg


def test_a_missing_sweep_is_absent_not_fatal(tmp_path):
    assert load_sweep(tmp_path) is None


def test_money_formatting_matches_the_cli():
    from ledgerlock.domain.money import fmt
    assert fmt_paise(48_721_350) == fmt(48_721_350) == "Rs 4,87,213.50"
    assert fmt_paise(None) == "Rs 0.00"


def test_board_survives_an_artifact_with_no_findings():
    empty = Board(recon={"links": [], "findings": [], "tiers_run": []},
                  score={"totals": {}, "links": {}})
    assert empty.by_action() == {}
    assert empty.codes_present() == []
    assert empty.filtered() == []
    assert empty.work_by_tier() == {}
    assert empty.audit("anything")["findings"] == []


# ---------------------------------------------------------------------------
# the app itself
# ---------------------------------------------------------------------------

def test_the_streamlit_app_renders_without_raising(tmp_path, monkeypatch):
    """Runs `app.py` end to end through Streamlit's own harness.

    An HTTP check only proves the shell HTML serves -- Streamlit executes the
    script over a websocket, so a NameError in a tab would still return 200.
    This actually runs it, against artefacts this test built, and fails on any
    exception the page would have shown a user.
    """
    st_testing = pytest.importorskip("streamlit.testing.v1",
                                     reason="streamlit extra not installed")
    from dataclasses import replace

    from ledgerlock import config
    from ledgerlock.eval.metrics import score, score_to_dict
    from ledgerlock.generate.engine import build
    from ledgerlock.generate.params import PROFILES
    from ledgerlock.generate.writer import write_world
    from ledgerlock.io.loaders import load_sources, load_truth
    from ledgerlock.llm.adapter import LLMClient, Mode
    from ledgerlock.llm.gemini import OfflineProvider
    from ledgerlock.pipeline.controller import reconcile_sources
    from ledgerlock.pipeline.result import Tier

    manifest = write_world(build(replace(PROFILES["smoke"], seed=42)), tmp_path)
    src = load_sources(tmp_path / "raw")
    client = LLMClient(OfflineProvider(), config.LLM_CACHE_DIR, mode=Mode.CACHED)
    result = reconcile_sources(src, upto=Tier.T3, llm=client)
    s = score(result, load_truth(tmp_path / "truth"), src, manifest)

    out = tmp_path / "out"
    out.mkdir()
    (out / "recon.json").write_text(result.model_dump_json(), encoding="utf-8")
    (out / "score.json").write_text(json.dumps(score_to_dict(s), default=str),
                                    encoding="utf-8")
    monkeypatch.setenv("LEDGERLOCK_OUT", str(out))

    at = st_testing.AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=90)
    at.run()

    assert not at.exception, f"app raised: {[e.value for e in at.exception]}"
    assert at.title[0].value == "LedgerLock"
    assert len(at.tabs) == 5
    # The headline metrics are present and read from score.json, not invented.
    labels = [m.label for m in at.metric]
    assert "false matches" in labels
    assert any("settlement" in l for l in labels)


def test_the_app_explains_itself_when_nothing_has_been_run(tmp_path, monkeypatch):
    """Opening the dashboard first is the likeliest first user action."""
    st_testing = pytest.importorskip("streamlit.testing.v1")
    monkeypatch.setenv("LEDGERLOCK_OUT", str(tmp_path / "nothing-here"))
    at = st_testing.AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception
    assert at.error, "no error shown for missing artefacts"
    assert "python -m ledgerlock run" in at.code[0].value


def test_the_app_renders_a_run_with_no_findings_at_all(tmp_path, monkeypatch):
    """An empty result must render an empty page, not crash.

    Found by hand: Streamlit raises StreamlitAPIException when a multiselect
    default is absent from its options, so a run with zero findings took down
    the whole queue tab. The data layer already handled it; the view did not.
    """
    st_testing = pytest.importorskip("streamlit.testing.v1")
    out = tmp_path / "out"
    out.mkdir()
    (out / "recon.json").write_text(json.dumps(
        {"links": [], "findings": [], "tiers_run": ["t1"],
         "explanations": {}, "llm": {}}), encoding="utf-8")
    (out / "score.json").write_text(json.dumps(
        {"profile": "smoke", "seed": 1, "n_records": 0,
         "links": {}, "totals": {}, "codes": [], "llm": {}}), encoding="utf-8")
    monkeypatch.setenv("LEDGERLOCK_OUT", str(out))

    at = st_testing.AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, f"empty run crashed: {[e.value for e in at.exception]}"
    assert at.title[0].value == "LedgerLock"


def test_the_view_refreshes_when_the_artifacts_change(tmp_path, monkeypatch):
    """Guards the cache-key bug: `@st.cache_data` ignores underscore-prefixed
    parameters, so a `_mtimes` argument pinned the first load forever and the
    dashboard would have shown stale numbers after every new pipeline run."""
    st_testing = pytest.importorskip("streamlit.testing.v1")
    out = tmp_path / "out"
    out.mkdir()

    def write(records: int) -> None:
        (out / "recon.json").write_text(json.dumps(
            {"links": [], "findings": [], "tiers_run": ["t1"],
             "explanations": {}, "llm": {}}), encoding="utf-8")
        (out / "score.json").write_text(json.dumps(
            {"profile": "smoke", "seed": 1, "n_records": records,
             "links": {}, "totals": {}, "codes": [], "llm": {}}), encoding="utf-8")

    monkeypatch.setenv("LEDGERLOCK_OUT", str(out))
    write(111)
    first = st_testing.AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=60)
    first.run()
    assert "111" in first.caption[0].value

    import time
    time.sleep(1.1)                      # a distinct mtime
    write(222)
    second = st_testing.AppTest.from_file(str(REPO_ROOT / "app.py"), default_timeout=60)
    second.run()
    assert "222" in second.caption[0].value, "the dashboard served stale data"
