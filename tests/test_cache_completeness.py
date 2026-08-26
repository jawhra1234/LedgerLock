"""The committed cache must cover every shipped profile.

This file exists because of F12. The cache was complete for `default` -- the
profile I was iterating on -- and stale for `smoke` and `scale`, and it passed
silently for days. Anyone cloning the repo and running those two profiles would
have got zero model answers, E12 reported as entirely undetected, and an empty
queue. The published `scale` figure would not have reproduced.

Nothing caught it because `--llm cached` treats a miss as a *normal* condition,
which is correct for a keyless clone and is exactly what hid the problem.

So the tolerance now has to be asked for explicitly, and these tests assert the
published artefact is whole. They use the real committed cache, not a fixture --
that is the point.
"""

from dataclasses import replace
from pathlib import Path

import pytest

from ledgerlock.generate.engine import build
from ledgerlock.generate.params import PROFILES
from ledgerlock.generate.writer import write_world
from ledgerlock.io.loaders import load_sources
from ledgerlock.llm.adapter import CacheIncomplete, LLMClient, Mode
from ledgerlock.llm.gemini import OfflineProvider
from ledgerlock.pipeline.controller import reconcile_sources
from ledgerlock.pipeline.result import Tier

REPO_ROOT = Path(__file__).resolve().parents[1]
CACHE = REPO_ROOT / "data" / "llm_cache"
SHIPPED_SEED = 42


def _run_cached(profile: str, tmp_path: Path) -> LLMClient:
    """Generate a shipped profile and reconcile it against the real cache.

    The provider is offline, so a cache miss cannot be papered over by an
    accidental network call.
    """
    world = build(replace(PROFILES[profile], seed=SHIPPED_SEED))
    write_world(world, tmp_path)
    src = load_sources(tmp_path / "raw")
    client = LLMClient(OfflineProvider(), CACHE, mode=Mode.CACHED)
    reconcile_sources(src, upto=Tier.T3, llm=client)
    return client


def test_the_committed_cache_exists():
    assert CACHE.is_dir(), f"{CACHE} is missing"
    assert list(CACHE.glob("*.json")), "the committed cache is empty"


@pytest.mark.parametrize("profile", ["smoke", "default", "scale"])
def test_every_shipped_profile_is_fully_cached(profile, tmp_path):
    """0 unanswered prompts, on every profile, at the shipped seed.

    This is the assertion whose absence let F12 ship-ready. If it fails, the
    fix is `run --upto t3 --llm live` on that profile, not a lowered bar.
    """
    client = _run_cached(profile, tmp_path)
    assert client.stats.misses == 0, (
        f"{profile}: {client.stats.misses} prompt(s) missing from the committed "
        f"cache -- the published numbers for this profile do not reproduce on a "
        f"clean clone. Regenerate with: python -m ledgerlock run --upto t3 --llm live"
    )
    assert client.stats.cache_hits > 0, f"{profile}: cache served nothing at all"
    # Offline provider: nothing may have reached the network.
    assert client.stats.calls_made == 0


@pytest.mark.parametrize("profile", ["smoke", "default", "scale"])
def test_cached_replay_answers_every_adjustment(profile, tmp_path):
    """Coverage measured against the work, not against the file count.

    Comparing cache file counts between two runs is what first raised the alarm
    and it was almost useless -- the caches simply held different questions. The
    only meaningful check is that every question this dataset asks has an answer.
    """
    world = build(replace(PROFILES[profile], seed=SHIPPED_SEED))
    write_world(world, tmp_path)
    src = load_sources(tmp_path / "raw")
    adjustments = [e for e in src.entries
                   if e.entry_type.value == "adjustment" and not e.order_id]
    assert adjustments

    client = LLMClient(OfflineProvider(), CACHE, mode=Mode.CACHED)
    reconcile_sources(src, upto=Tier.T3, llm=client)
    # Every adjustment asked about, plus the queue's group and item notes.
    assert client.stats.cache_hits >= len(adjustments)


def test_assert_complete_raises_on_a_miss(tmp_path):
    client = LLMClient(OfflineProvider(), tmp_path, mode=Mode.CACHED)
    assert client.ask("never seen", {"x": 1}) is None
    with pytest.raises(CacheIncomplete) as e:
        client.assert_complete()
    # The message has to say what to do about it, not just that it happened.
    assert "--llm live" in str(e.value)
    assert "--allow-cache-miss" in str(e.value)


def test_assert_complete_is_silent_when_the_cache_is_whole(tmp_path):
    client = _run_cached("smoke", tmp_path)
    client.assert_complete()          # must not raise


def test_off_mode_is_not_held_to_cache_completeness(tmp_path):
    """`--llm off` is a legitimate way to run: no tier 3, so nothing to cache."""
    world = build(replace(PROFILES["smoke"], seed=SHIPPED_SEED))
    write_world(world, tmp_path)
    src = load_sources(tmp_path / "raw")
    client = LLMClient(OfflineProvider(), tmp_path / "empty", mode=Mode.OFF)
    reconcile_sources(src, upto=Tier.T3, llm=client)
    client.assert_complete()          # must not raise
    assert client.stats.misses == 0


def test_live_mode_is_not_held_to_cache_completeness(tmp_path):
    """A live run is allowed to miss -- missing is how it finds work to do."""
    client = LLMClient(OfflineProvider(), tmp_path, mode=Mode.LIVE)
    client.ask("q", {"x": 1})
    client.assert_complete()          # must not raise
