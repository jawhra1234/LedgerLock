"""Tests for the model boundary and tier 3.

Nothing here touches the network. The provider is faked, the cache is a tmpdir,
and the Gemini transport is exercised by monkeypatching `httpx.post` -- so `pytest`
works on a keyless clone, offline, forever.

The structural claim being tested: **T3 emits no links.** A tier that cannot
create a link cannot create a false match, so the project's headline guarantee
holds by construction rather than by the model behaving well today.
"""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from ledgerlock import config
from ledgerlock.domain.taxonomy import ExceptionCode as EC
from ledgerlock.generate.engine import build
from ledgerlock.generate.narrations import BENIGN_ADJUSTMENTS, OPAQUE_ADJUSTMENTS
from ledgerlock.generate.params import PROFILES
from ledgerlock.generate.writer import write_world
from ledgerlock.io.loaders import load_sources, load_truth
from ledgerlock.llm import prompts
from ledgerlock.llm.adapter import LLMClient, Mode, prompt_key
from ledgerlock.llm.gemini import GeminiProvider, OfflineProvider, build_provider
from ledgerlock.pipeline.controller import reconcile_sources
from ledgerlock.pipeline.result import Action, Tier


class FakeProvider:
    """Answers from a lookup table, and counts what it was asked."""

    name = "fake"

    def __init__(self, answers=None, always=None):
        self.answers = answers or {}
        self.always = always
        self.prompts: list[str] = []

    def generate(self, prompt, schema):
        self.prompts.append(prompt)
        if self.always is not None:
            return dict(self.always), self.name
        for needle, answer in self.answers.items():
            if needle in prompt:
                return dict(answer), self.name
        return None, "fake: no canned answer"


OPAQUE_ANSWER = {"explains_purpose": False, "category": "internal reference",
                 "confidence": 0.95, "evidence": "REF"}
BENIGN_ANSWER = {"explains_purpose": True, "category": "goodwill",
                 "confidence": 0.95, "evidence": "credit"}


# ---------------------------------------------------------------------------
# the adapter
# ---------------------------------------------------------------------------

def test_prompt_key_is_stable_and_content_addressed():
    a = prompt_key("hello", {"type": "object"})
    b = prompt_key("hello", {"type": "object"})
    c = prompt_key("hello!", {"type": "object"})
    assert a == b and a != c
    assert len(a) == 32


def test_off_mode_never_asks_anything():
    p = FakeProvider(always=OPAQUE_ANSWER)
    c = LLMClient(p, Path("nonexistent"), mode=Mode.OFF)
    assert c.ask("anything", {}) is None
    assert p.prompts == []


def test_cached_mode_never_calls_the_provider(tmp_path):
    """The default. A miss is a miss, not a reason to reach for the network."""
    p = FakeProvider(always=OPAQUE_ANSWER)
    c = LLMClient(p, tmp_path, mode=Mode.CACHED)
    assert c.ask("q", {"x": 1}) is None
    assert p.prompts == []
    assert c.stats.misses == 1


def test_live_mode_writes_a_cache_entry_that_cached_mode_can_read(tmp_path):
    p = FakeProvider(always=OPAQUE_ANSWER)
    live = LLMClient(p, tmp_path, mode=Mode.LIVE)
    assert live.ask("q", {"x": 1}) == OPAQUE_ANSWER
    assert live.stats.calls_made == 1

    # A second client, offline, with no provider that could answer.
    cached = LLMClient(OfflineProvider(), tmp_path, mode=Mode.CACHED)
    assert cached.ask("q", {"x": 1}) == OPAQUE_ANSWER
    assert cached.stats.cache_hits == 1
    assert cached.stats.calls_made == 0


def test_cache_entries_are_byte_stable(tmp_path):
    """The cache is committed, so writing it twice must not dirty the diff."""
    p = FakeProvider(always=OPAQUE_ANSWER)
    LLMClient(p, tmp_path, mode=Mode.LIVE).ask("q", {"x": 1})
    first = {f.name: f.read_bytes() for f in tmp_path.iterdir()}
    for f in tmp_path.iterdir():
        f.unlink()
    LLMClient(p, tmp_path, mode=Mode.LIVE).ask("q", {"x": 1})
    assert {f.name: f.read_bytes() for f in tmp_path.iterdir()} == first


def test_call_budget_truncates_and_says_so(tmp_path):
    p = FakeProvider(always=OPAQUE_ANSWER)
    c = LLMClient(p, tmp_path, mode=Mode.LIVE, max_calls=2)
    for i in range(5):
        c.ask(f"q{i}", {"x": 1})
    assert c.stats.calls_made == 2
    assert c.stats.budget_truncated == 3      # never silent


def test_provider_failure_is_recorded_not_swallowed(tmp_path):
    c = LLMClient(FakeProvider(answers={}), tmp_path, mode=Mode.LIVE)
    assert c.ask("nothing canned", {"x": 1}) is None
    assert c.stats.failures == 1
    assert c.notes


def test_corrupt_cache_file_is_ignored_not_fatal(tmp_path):
    key = prompt_key("q", {"x": 1})
    (tmp_path / f"{key}.json").write_text("{not json", encoding="utf-8")
    c = LLMClient(OfflineProvider(), tmp_path, mode=Mode.CACHED)
    assert c.ask("q", {"x": 1}) is None


# ---------------------------------------------------------------------------
# the Gemini transport, without a network
# ---------------------------------------------------------------------------

def _resp(status, payload):
    return httpx.Response(status, json=payload,
                          request=httpx.Request("POST", "https://x"))


def test_offline_provider_declines_cleanly():
    answer, note = OfflineProvider().generate("q", {})
    assert answer is None
    assert "offline" in note


def test_build_provider_falls_back_without_a_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert build_provider(prefer_live=True).name == "offline"
    assert build_provider(prefer_live=False).name == "offline"


def test_a_saturated_model_falls_through_to_the_next(monkeypatch):
    """Probing a real key showed three Flash tiers failing three different ways
    inside one minute. Naming one model would mean dying when it is busy."""
    seen = []

    def fake_post(url, **kw):
        model = url.split("/models/")[1].split(":")[0]
        seen.append(model)
        if model == "m1":
            return _resp(503, {"error": {"status": "UNAVAILABLE"}})
        return _resp(200, {"candidates": [{"content": {"parts": [
            {"text": '{"ok": true}'}]}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    p = GeminiProvider(api_key="k", chain=("m1", "m2"), retries=2, sleep=lambda _: None)
    answer, note = p.generate("q", {})
    assert answer == {"ok": True}
    assert note == "m2"
    assert seen == ["m1", "m1", "m2"]        # retried m1, then moved on


def test_throttling_is_retried_within_a_model(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            return _resp(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})
        return _resp(200, {"candidates": [{"content": {"parts": [
            {"text": '{"ok": 1}'}]}}]})

    monkeypatch.setattr(httpx, "post", fake_post)
    p = GeminiProvider(api_key="k", chain=("m1",), retries=3, sleep=lambda _: None)
    assert p.generate("q", {})[0] == {"ok": 1}
    assert calls["n"] == 2


def test_a_404_moves_on_without_retrying(monkeypatch):
    calls = {"n": 0}

    def fake_post(url, **kw):
        calls["n"] += 1
        return _resp(404, {"error": {"status": "NOT_FOUND"}})

    monkeypatch.setattr(httpx, "post", fake_post)
    p = GeminiProvider(api_key="k", chain=("gone",), retries=3, sleep=lambda _: None)
    assert p.generate("q", {})[0] is None
    assert calls["n"] == 1          # a missing model is not a transient failure


def test_unparseable_json_is_no_answer_never_a_guess(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(
        200, {"candidates": [{"content": {"parts": [{"text": "not json"}]}}]}))
    p = GeminiProvider(api_key="k", chain=("m",), retries=1, sleep=lambda _: None)
    answer, note = p.generate("q", {})
    assert answer is None
    assert "unparseable" in note


def test_network_error_is_survived(monkeypatch):
    def boom(url, **kw):
        raise httpx.ConnectTimeout("no route")

    monkeypatch.setattr(httpx, "post", boom)
    p = GeminiProvider(api_key="k", chain=("m",), retries=2, sleep=lambda _: None)
    assert p.generate("q", {})[0] is None


# ---------------------------------------------------------------------------
# prompts
# ---------------------------------------------------------------------------

def test_prompts_leak_no_ground_truth(tmp_path):
    """A prompt may only contain what data/raw/ already exposes.

    The TruthLeak guard makes reading the answer key structurally hard; this
    makes the promise explicit at the boundary where text leaves the building.
    """
    world = build(replace(PROFILES["smoke"], seed=42))
    write_world(world, tmp_path)
    truth = load_truth(tmp_path / "truth")
    codes = {x.code.value for x in truth.exceptions}

    text = prompts.adjustment_prompt("MISC DR REF 88213", -12345, "STL_0001")
    text += prompts.item_explanation_prompt("entry:ENT_1", "Duplicate payment",
                                            500, "identical capture")
    for token in codes | {"resolvability", "truth_", "is_resolvable"}:
        assert token not in text


def test_adjustment_prompt_states_both_directions():
    credit = prompts.adjustment_prompt("x", 500, None)
    debit = prompts.adjustment_prompt("x", -500, None)
    assert "credit to the merchant" in credit
    assert "debit from the merchant" in debit
    assert "not yet settled" in credit


def test_narration_vocabulary_is_wide_and_disjoint():
    """A classifier tested on the only 7 strings it will ever see proves
    nothing. See D15 in DECISIONS.md."""
    assert len(BENIGN_ADJUSTMENTS) >= 12
    assert len(OPAQUE_ADJUSTMENTS) >= 15
    assert not set(BENIGN_ADJUSTMENTS) & set(OPAQUE_ADJUSTMENTS)


# ---------------------------------------------------------------------------
# tier 3 behaviour
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def world(tmp_path_factory):
    root = tmp_path_factory.mktemp("t3world")
    w = build(replace(PROFILES["default"], seed=42))
    write_world(w, root)
    return load_sources(root / "raw"), load_truth(root / "truth")


def _run(src, provider, tmp_path, mode=Mode.LIVE):
    client = LLMClient(provider, tmp_path, mode=mode)
    return reconcile_sources(src, upto=Tier.T3, llm=client), client


def test_t3_emits_no_links_ever(world, tmp_path):
    """The structural guarantee. Even told to flag everything, T3 adds no link."""
    src, _ = world
    t2 = reconcile_sources(src, upto=Tier.T2)
    result, _ = _run(src, FakeProvider(always=OPAQUE_ANSWER), tmp_path)
    assert len(result.links) == len(t2.links)
    assert not [l for l in result.links if l.tier is Tier.T3]


def test_t3_flags_opaque_adjustments_and_always_escalates(world, tmp_path):
    src, _ = world
    result, _ = _run(src, FakeProvider(always=OPAQUE_ANSWER), tmp_path)
    found = result.findings_of(EC.UNEXPLAINED_ADJUSTMENT)
    assert found
    # Resolvability is `none`: the model decides whether a human looks, never
    # whether the case closes.
    assert all(f.action is Action.ESCALATED for f in found)
    assert all(f.tier is Tier.T3 for f in found)


def test_a_model_that_calls_everything_explanatory_flags_nothing(world, tmp_path):
    src, _ = world
    result, _ = _run(src, FakeProvider(always=BENIGN_ANSWER), tmp_path)
    assert result.findings_of(EC.UNEXPLAINED_ADJUSTMENT) == []


def test_low_confidence_answers_are_discarded(world, tmp_path):
    src, _ = world
    timid = dict(OPAQUE_ANSWER, confidence=config.T3_MIN_CONFIDENCE - 0.01)
    result, _ = _run(src, FakeProvider(always=timid), tmp_path)
    assert result.findings_of(EC.UNEXPLAINED_ADJUSTMENT) == []


def test_no_model_means_no_claim(world, tmp_path):
    """Offline is a supported way to run, not a broken one."""
    src, _ = world
    result, client = _run(src, OfflineProvider(), tmp_path)
    assert result.findings_of(EC.UNEXPLAINED_ADJUSTMENT) == []
    assert result.explanations == {}
    assert client.stats.failures > 0
    # And everything the deterministic tiers proved still stands.
    assert len(result.links) == len(reconcile_sources(src, upto=Tier.T2).links)


def test_t3_asks_about_benign_adjustments_too(world, tmp_path):
    """Asking only about the ones already known to be opaque would be scoring
    the model against an answer key it was handed."""
    src, _ = world
    p = FakeProvider(always=BENIGN_ANSWER)
    _run(src, p, tmp_path)
    asked = "\n".join(p.prompts)
    benign_seen = sum(1 for n in BENIGN_ADJUSTMENTS if n in asked)
    assert benign_seen >= 1


def test_every_adjustment_with_no_order_is_asked_about(world, tmp_path):
    src, _ = world
    p = FakeProvider(always=BENIGN_ANSWER)
    _run(src, p, tmp_path)
    subjects = [e for e in src.entries
                if e.entry_type.value == "adjustment" and not e.order_id]
    asked = "\n".join(p.prompts)
    assert all(e.narration in asked for e in subjects)


def test_explanations_are_generated_but_never_scored(world, tmp_path):
    from ledgerlock.eval.metrics import score

    src, truth = world
    p = FakeProvider(always={"explanation": "A thing happened.",
                             "next_step": "Look at it."})
    result, _ = _run(src, p, tmp_path)
    assert result.explanations                       # produced
    s = score(result, truth, src)
    # A readable sentence is not a match, and does not move any metric.
    assert s.total_fp == 0
    assert s.links["settlement_bank"].recall == 1.0


def test_t3_never_makes_the_deterministic_result_worse(world, tmp_path):
    """Whatever the model says, T1+T2's proven output is untouched."""
    from ledgerlock.eval.metrics import score

    src, truth = world
    t2 = score(reconcile_sources(src, upto=Tier.T2), truth, src)
    hostile, _ = _run(src, FakeProvider(always=OPAQUE_ANSWER), tmp_path)
    t3 = score(hostile, truth, src)
    assert t3.links["settlement_bank"].recall >= t2.links["settlement_bank"].recall
    assert t3.total_fp == t2.total_fp == 0
    assert t3.unresolvable_auto_resolved == []
