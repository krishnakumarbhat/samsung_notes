"""Bounded Gemini reasoning requests; never used for local corpus embeddings."""
from __future__ import annotations

import json
import os
import random
import time
import urllib.error
import urllib.request
from pathlib import Path


class GeminiError(RuntimeError):
    """Sanitized provider/configuration failure with no credentials or note text."""


class _EmptyResponse(GeminiError):
    """The provider returned a candidate with no usable text; try the fallback."""


class GeminiClient:
    """Root-.env configuration, retry transient failures, fallback on unavailable models."""

    def __init__(self, *, root: Path | None = None, api_key: str | None = None,
                 primary_model: str | None = None, fallback_model: str | None = None,
                 max_retries: int | None = None, initial_backoff_sec: float | None = None,
                 backoff_multiplier: float | None = None) -> None:
        explicit_root = root is not None
        root = root or Path(__file__).resolve().parents[3]
        files = [root / ".env", Path(__file__).resolve().parents[1] / ".env"]
        if not explicit_root:
            # NTFS ignores chmod; real credentials live in a 0600 home file.
            secure = Path(os.environ.get(
                "SAM_ENV_FILE", Path.home() / ".config/sam_win/.env"))
            files.insert(0, secure)
        values: dict[str, str] = {}
        for file in files:
            if file.is_file():
                for line in file.read_text().splitlines():
                    if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                        key, value = line.split("=", 1)
                        value = value.strip().strip("\"'")
                        if value:
                            values.setdefault(key.strip(), value)
        values.update({key: value for key, value in os.environ.items() if value})
        self.api_key = api_key if api_key is not None else (values.get("GEMINI_API_KEY") or values.get("GOOGLE_API_KEY", ""))
        self.primary_model = primary_model or values.get("GEMINI_PRIMARY_MODEL", "gemini-3.8-flash")
        self.fallback_model = fallback_model or values.get("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite")
        self.max_retries = max(0, min(5, int(max_retries if max_retries is not None else values.get("GEMINI_MAX_RETRIES", "5"))))
        self.initial_backoff = max(0., float(initial_backoff_sec if initial_backoff_sec is not None else values.get("GEMINI_INITIAL_BACKOFF_SEC", "2")))
        self.backoff_multiplier = max(1., float(backoff_multiplier if backoff_multiplier is not None else values.get("GEMINI_BACKOFF_MULTIPLIER", "2")))
        self.budget = min(1900, int(values.get("GEMINI_PROMPT_TOKEN_BUDGET", "1800")))

    def _post(self, model: str, operation: str, body: dict, timeout: int) -> dict:
        """POST using header credentials, with no provider body in logged exceptions."""
        request = urllib.request.Request(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:{operation}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "x-goog-api-key": self.api_key},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)

    def _generate_once(self, model: str, prompt: str, system: str, temperature: float,
                       max_output_tokens: int, timeout: int, *, response_json: bool = False,
                       response_schema: dict | None = None) -> str:
        """Count the actual prompt tokens before requesting any generation."""
        body: dict = {"contents": [{"parts": [{"text": prompt}]}]}
        if system:
            body["systemInstruction"] = {"parts": [{"text": system}]}
        body["generationConfig"] = {"temperature": temperature, "maxOutputTokens": max_output_tokens}
        if response_json:
            body["generationConfig"]["responseMimeType"] = "application/json"
        if response_schema:
            body["generationConfig"]["responseSchema"] = response_schema
        count_body = {"generateContentRequest": {"model": f"models/{model}", **body}}
        count = self._post(model, "countTokens", count_body, timeout)
        if int(count.get("totalTokens", self.budget + 1)) > self.budget:
            raise GeminiError("Prompt exceeds strict token budget; narrow the source selection")
        output = self._post(model, "generateContent", body, timeout)
        candidates = output.get("candidates") or []
        parts = candidates[0].get("content", {}).get("parts", []) if candidates else []
        text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
        if not text:
            raise _EmptyResponse("Provider returned no usable answer")
        return text

    def generate(self, prompt: str, *, system: str = "", temperature: float = .4,
                 max_output_tokens: int = 700, timeout: int = 60, response_json: bool = False,
                 response_schema: dict | None = None) -> str:
        """Retry with jitter on 429/5xx/timeouts; never retry authentication errors."""
        if not self.api_key or self.api_key.startswith("your_"):
            raise GeminiError("Configure GEMINI_API_KEY in the root .env")
        for model in dict.fromkeys((self.primary_model, self.fallback_model)):
            for attempt in range(self.max_retries + 1):
                try:
                    return self._generate_once(model, prompt, system, temperature, max_output_tokens, timeout,
                                               response_json=response_json, response_schema=response_schema)
                except _EmptyResponse:
                    break  # unusable primary answer: switch models now
                except urllib.error.HTTPError as error:
                    status = error.code
                    if status in (401, 403):
                        raise GeminiError(f"Gemini authentication/access denied (HTTP {status}); retries stopped") from None
                    if status == 400:
                        detail = error.read(8192).decode(errors="replace")
                        if "API_KEY_INVALID" in detail or "API key not valid" in detail:
                            raise GeminiError("Gemini API key rejected; retries stopped") from None
                    if status in (400, 404):
                        break  # unavailable model or incompatible model request: try configured fallback
                    if status != 429 and not 500 <= status <= 599:
                        raise GeminiError(f"Gemini request failed (HTTP {status})") from None
                except (urllib.error.URLError, TimeoutError, OSError):
                    pass
                # A dead/misconfigured primary must not consume the whole budget:
                # after two quick failures move to the fallback model.
                if model == self.primary_model and model != self.fallback_model and attempt >= 1:
                    break
                if attempt < self.max_retries:
                    delay = min(30., self.initial_backoff * self.backoff_multiplier**attempt)
                    time.sleep(delay + random.uniform(0., delay * .25))
        raise GeminiError("Configured Gemini models unavailable after bounded retries")

    def generate_json(self, prompt: str, **kwargs: object) -> dict:
        """Parse a JSON object and reject malformed model output without echoing it."""
        text = self.generate(prompt, response_json=True, **kwargs)
        try:
            value = json.loads(text[text.index("{"):text.rindex("}") + 1])
        except (ValueError, TypeError):
            raise GeminiError("Provider returned invalid JSON; no learning state was changed") from None
        if not isinstance(value, dict):
            raise GeminiError("Expected a JSON response object")
        return value
