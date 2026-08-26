"""Gemini provider, over the REST API with httpx.

No SDK dependency. The request is a dict and the response is JSON; pulling in a
client library to post one endpoint would be a dependency for nothing, and it
would make the adapter harder to swap rather than easier.

Two behaviours learned from probing a live free-tier key rather than from
documentation: models return 503 when saturated and 429 when throttled, and
which tier is healthy changes minute to minute. So this retries with backoff
inside a model, then falls through to the next model in the chain, and reports
which one actually answered.
"""

from __future__ import annotations

import json
import os
import time

import httpx

from .. import config

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
RETRYABLE = {429, 500, 502, 503, 504}


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        api_key: str | None = None,
        chain: tuple[str, ...] | None = None,
        retries: int = config.LLM_RETRIES_PER_MODEL,
        timeout: int = config.LLM_TIMEOUT_SECONDS,
        sleep=time.sleep,
    ) -> None:
        self.api_key = api_key or os.getenv("GEMINI_API_KEY", "")
        env_model = os.getenv("LEDGERLOCK_LLM_MODEL", "").strip()
        base = chain or config.LLM_MODEL_CHAIN
        # An explicitly configured model leads the chain; the configured
        # fallbacks stay behind it.
        self.chain = (env_model, *[m for m in base if m != env_model]) if env_model else base
        self.retries = retries
        self.timeout = timeout
        self._sleep = sleep

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def _post(self, model: str, prompt: str, schema: dict) -> httpx.Response:
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                # Temperature 0 is not determinism -- the committed cache is
                # what makes the score reproducible. This just removes the
                # avoidable variance.
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": schema,
            },
        }
        return httpx.post(
            ENDPOINT.format(model=model),
            headers={"x-goog-api-key": self.api_key,
                     "content-type": "application/json"},
            json=body,
            timeout=self.timeout,
        )

    def generate(self, prompt: str, schema: dict) -> tuple[dict | None, str]:
        if not self.available:
            return None, "no GEMINI_API_KEY in the environment"

        last = ""
        for model in self.chain:
            for attempt in range(self.retries):
                try:
                    r = self._post(model, prompt, schema)
                except httpx.HTTPError as e:                  # network, DNS, timeout
                    last = f"{model}: {type(e).__name__}"
                    self._sleep(2 ** attempt)
                    continue

                if r.status_code == 200:
                    try:
                        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
                        return json.loads(text), model
                    except (KeyError, IndexError, json.JSONDecodeError) as e:
                        # A malformed answer is no answer. Never a guess.
                        return None, f"{model}: unparseable response ({type(e).__name__})"

                status = ""
                try:
                    status = r.json().get("error", {}).get("status", "")
                except Exception:
                    pass
                last = f"{model}: HTTP {r.status_code} {status}".strip()

                if r.status_code in RETRYABLE:
                    self._sleep(2 ** attempt)
                    continue
                break                     # 400/403/404: next model, not a retry

        return None, f"model chain exhausted ({last})"


class OfflineProvider:
    """What a keyless clone and CI use. Answers nothing, deterministically.

    Not a stub for tests -- it is the supported way to run this project. With
    the committed cache in place it never gets asked; on a genuine cache miss
    it declines, and the finding stays honestly unnamed instead of the run
    failing or, worse, inventing an answer.
    """

    name = "offline"

    def generate(self, prompt: str, schema: dict) -> tuple[dict | None, str]:
        return None, "offline provider: no model consulted"


def build_provider(prefer_live: bool):
    """Pick a provider. Live only when asked for *and* a key exists."""
    if not prefer_live:
        return OfflineProvider()
    g = GeminiProvider()
    return g if g.available else OfflineProvider()
