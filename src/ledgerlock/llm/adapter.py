"""The model boundary.

The interface is one method wide on purpose. A model does roughly 3% of the
work in this pipeline, so nothing structural is allowed to depend on which one
it is -- swapping provider or tier is a config line, and the offline provider is
a first-class citizen rather than a test double.

`LLMClient` wraps a provider with the three things that make a non-deterministic
component safe to put inside a project whose claim is reproducibility:

  * a content-addressed cache, committed to the repo, so `eval` reproduces the
    published numbers on a clean clone with no API key at all
  * a hard call budget, with truncation reported rather than silent
  * schema validation, where a malformed answer is *no answer* and never a guess
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from .. import config


class CacheIncomplete(RuntimeError):
    """The committed cache does not cover this dataset.

    Exists because of F12: a cache that was complete for one profile and stale
    for two others passed silently for days, because `cached` mode treats a miss
    as a normal condition. That tolerance is right for a keyless clone and it is
    exactly what hid the problem, so the tolerance now has to be asked for.
    """


class Mode(StrEnum):
    OFF = "off"          # T3 does not run
    CACHED = "cached"    # cache only; a miss is not an error
    LIVE = "live"        # call the provider on a miss, and cache the answer


class Provider(Protocol):
    name: str

    def generate(self, prompt: str, schema: dict) -> tuple[dict | None, str]:
        """-> (parsed answer or None, note). `note` explains a None."""


def prompt_key(prompt: str, schema: dict) -> str:
    """Content address for a request.

    Deliberately excludes the model. A fallback model's answer to an identical
    question is still a valid cached answer, so a busy primary model does not
    churn the committed cache. Which model actually answered is recorded inside
    the entry.
    """
    blob = json.dumps({"prompt": prompt, "schema": schema},
                      sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


@dataclass
class Stats:
    cache_hits: int = 0
    calls_made: int = 0
    misses: int = 0          # cached mode, nothing on disk
    failures: int = 0        # provider asked, gave nothing usable
    budget_truncated: int = 0
    models_used: dict[str, int] = field(default_factory=dict)

    @property
    def answered(self) -> int:
        return self.cache_hits + self.calls_made

    def as_dict(self) -> dict:
        return {
            "cache_hits": self.cache_hits,
            "calls_made": self.calls_made,
            "cache_misses_unanswered": self.misses,
            "provider_failures": self.failures,
            "budget_truncated": self.budget_truncated,
            "models_used": dict(sorted(self.models_used.items())),
        }


class LLMClient:
    def __init__(
        self,
        provider: Provider,
        cache_dir: Path,
        mode: Mode = Mode.CACHED,
        max_calls: int = config.LLM_MAX_CALLS_PER_RUN,
    ) -> None:
        self.provider = provider
        self.cache_dir = Path(cache_dir)
        self.mode = mode
        self.max_calls = max_calls
        self.stats = Stats()
        self.notes: list[str] = []

    # -- cache -------------------------------------------------------------
    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{key}.json"

    def _read(self, key: str) -> dict | None:
        p = self._path(key)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _write(self, key: str, prompt: str, model: str, answer: dict) -> None:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        # No timestamps: the cache is committed, so it has to be byte-stable
        # across regenerations or every run dirties the diff.
        self._path(key).write_text(
            json.dumps({"prompt": prompt, "model": model, "answer": answer},
                       indent=2, sort_keys=True),
            encoding="utf-8", newline="\n")

    # -- the one entry point ----------------------------------------------
    def ask(self, prompt: str, schema: dict) -> dict | None:
        if self.mode is Mode.OFF:
            return None

        key = prompt_key(prompt, schema)
        hit = self._read(key)
        if hit is not None:
            self.stats.cache_hits += 1
            model = hit.get("model", "cache")
            self.stats.models_used[model] = self.stats.models_used.get(model, 0) + 1
            return hit.get("answer")

        if self.mode is Mode.CACHED:
            self.stats.misses += 1
            return None

        if self.stats.calls_made >= self.max_calls:
            self.stats.budget_truncated += 1
            return None

        answer, note = self.provider.generate(prompt, schema)
        if answer is None:
            self.stats.failures += 1
            if note and note not in self.notes:
                self.notes.append(note)
            return None

        self.stats.calls_made += 1
        model = note or self.provider.name
        self.stats.models_used[model] = self.stats.models_used.get(model, 0) + 1
        self._write(key, prompt, model, answer)
        return answer

    def assert_complete(self) -> None:
        """Fail loudly when cached mode could not answer something.

        A silent miss means the published numbers do not reproduce for whoever
        cloned the repo -- the exact failure this project exists to avoid.
        """
        if self.mode is Mode.CACHED and self.stats.misses:
            raise CacheIncomplete(
                f"{self.stats.misses} prompt(s) are not in {self.cache_dir}. "
                "The committed cache does not cover this dataset, so any score "
                "from this run is incomplete. Regenerate with `--llm live` "
                "(needs GEMINI_API_KEY), skip the tier with `--llm off`, or "
                "pass --allow-cache-miss to accept the gap deliberately."
            )

    def summary(self) -> dict:
        d = self.stats.as_dict()
        d["mode"] = self.mode.value
        d["provider"] = self.provider.name
        if self.notes:
            d["notes"] = self.notes
        return d
