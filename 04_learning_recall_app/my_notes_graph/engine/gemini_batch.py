"""Gemini transport for the micro-batch question factory.

The study app already has a retrying client for interactive answers.  Batch
generation has different needs: it asks for *thinking* output, it must survive
long rate-limit windows without losing a checkpoint, and it must be testable
without the network.  This module therefore exposes one narrow method,
:meth:`BatchGemini.json`, with full-jitter exponential backoff and an injectable
transport.
"""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request

from src.config import GEMINI_KEY

PRIMARY_MODEL = os.environ.get("QUIZ_PRIMARY_MODEL", "gemini-3.8-flash")
FALLBACK_MODEL = os.environ.get("QUIZ_FALLBACK_MODEL", "gemini-3.5-flash-lite")
THINKING_BUDGET = int(os.environ.get("QUIZ_THINKING_BUDGET", "1024"))


class BatchError(RuntimeError):
    """A batch call failed after every model and every bounded retry."""


class BatchGemini:
    """Minimal batch client: JSON in, JSON out, bounded retries, model fallback."""

    def __init__(self, *, api_key: str | None = None,
                 models: tuple[str, ...] | None = None,
                 thinking_budget: int = THINKING_BUDGET,
                 max_attempts: int = 4, base_delay: float = 2.0,
                 cap_delay: float = 45.0, timeout: int = 180,
                 transport=None, sleep=time.sleep, rng: random.Random | None = None,
                 endpoint: str = "https://generativelanguage.googleapis.com/v1beta/models"):
        self.api_key = api_key if api_key is not None else GEMINI_KEY
        self.models = models or (PRIMARY_MODEL, FALLBACK_MODEL)
        self.thinking_budget = thinking_budget
        self.max_attempts = max(1, int(max_attempts))
        self.base_delay = max(0.0, float(base_delay))
        self.cap_delay = max(0.5, float(cap_delay))
        self.timeout = timeout
        self._transport = transport
        self._sleep = sleep
        self._rng = rng or random.Random()
        self.endpoint = endpoint

    def _post(self, model: str, body: dict) -> dict:
        """POST one generateContent call; let HTTP errors surface to the retry loop."""
        request = urllib.request.Request(
            f"{self.endpoint}/{model}:generateContent",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response)

    def _delay(self, attempt: int, retry_after: float | None = None) -> float:
        """Full-jitter backoff: uniform(0, min(cap, base·2^attempt))."""
        if retry_after is not None:
            return min(self.cap_delay, max(0.0, retry_after)) + self._rng.uniform(0, 0.5)
        window = min(self.cap_delay, self.base_delay * (2 ** attempt))
        return self._rng.uniform(0.0, window)

    def _body(self, prompt: str, system: str, temperature: float,
              max_output_tokens: int) -> dict:
        body: dict = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        generation: dict = {
            "temperature": temperature,
            "maxOutputTokens": max_output_tokens,
            "responseMimeType": "application/json",
        }
        if self.thinking_budget > 0:
            generation["thinkingConfig"] = {
                "thinkingBudget": self.thinking_budget,
                "includeThoughts": False,
            }
        body["generationConfig"] = generation
        return body

    def json(self, prompt: str, *, system: str = "", temperature: float = 0.55,
             max_output_tokens: int = 4096) -> dict:
        """Return one parsed JSON object, retrying 429/timeouts with full jitter."""
        if not self.api_key or self.api_key.startswith("your_"):
            raise BatchError("Configure GEMINI_API_KEY before running the quiz factory")
        body = self._body(prompt, system, temperature, max_output_tokens)
        failures: list[str] = []
        for model in dict.fromkeys(self.models):
            for attempt in range(self.max_attempts):
                try:
                    payload = (self._transport(model, body) if self._transport
                               else self._post(model, body))
                    return self._parse(payload)
                except urllib.error.HTTPError as error:
                    status = error.code
                    detail = ""
                    try:
                        detail = error.read(4096).decode(errors="replace")
                    except Exception:
                        detail = ""
                    if status in (401, 403):
                        raise BatchError(
                            f"Gemini rejected the credentials (HTTP {status}); retries stopped"
                        ) from None
                    if status in (400, 404):
                        failures.append(f"{model}: HTTP {status}")
                        break  # unusable model: move to the fallback immediately
                    if status != 429 and not 500 <= status <= 599:
                        raise BatchError(f"Gemini batch call failed (HTTP {status})") from None
                    failures.append(f"{model}: HTTP {status}")
                    retry_after = None
                    header = error.headers.get("Retry-After") if error.headers else None
                    if header:
                        try:
                            retry_after = float(header)
                        except ValueError:
                            retry_after = None
                    if attempt + 1 < self.max_attempts:
                        self._sleep(self._delay(attempt, retry_after))
                except (urllib.error.URLError, TimeoutError, OSError):
                    failures.append(f"{model}: network")
                    if attempt + 1 < self.max_attempts:
                        self._sleep(self._delay(attempt))
                except ValueError as error:
                    failures.append(f"{model}: {error}")
                    if attempt + 1 < self.max_attempts:
                        self._sleep(self._delay(attempt))
        raise BatchError("Quiz batch generation failed after retries: " + "; ".join(failures[-4:]))

    @staticmethod
    def _parse(payload: dict) -> dict:
        """Extract the JSON object from a generateContent response."""
        candidates = payload.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if not text.strip():
            raise ValueError("empty model response")
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("model response was not a JSON object")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as error:
            raise ValueError(f"model response was not valid JSON ({error.msg})") from None
        if not isinstance(value, dict):
            raise ValueError("model response was not a JSON object")
        return value
