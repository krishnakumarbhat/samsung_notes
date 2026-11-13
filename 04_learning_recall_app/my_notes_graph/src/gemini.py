"""Compatibility adapter: Gemini is reserved for reasoning, never embeddings."""
from __future__ import annotations
import math
import re
from src.config import PROMPT_TOKEN_BUDGET
from src.gemini_client import GeminiClient, GeminiError


def estimated_tokens(text: str) -> int:
    """Estimate prompt packing locally; client enforces actual countTokens too."""
    return max(len(re.findall(r"\w+|[^\w\s]", text)), math.ceil(len(text) / 3))


def validate_prompt_budget(prompt: str, system: str = "") -> int:
    """Reject obviously oversized prompts before the exact remote count."""
    estimate = estimated_tokens(prompt) + estimated_tokens(system)
    if estimate > PROMPT_TOKEN_BUDGET:
        raise GeminiError("Prompt budget exceeded; reduce selected context")
    return estimate


def generate(prompt: str, system: str = "", temperature: float = .4,
             max_output_tokens: int = 700) -> str:
    """Generate through the tested root-configured retry/fallback client."""
    validate_prompt_budget(prompt, system)
    return GeminiClient().generate(prompt, system=system, temperature=temperature,
                                   max_output_tokens=max_output_tokens)


def generate_json(prompt: str, system: str = "", temperature: float = .4,
                  max_output_tokens: int = 700, response_schema: dict | None = None) -> dict:
    """Generate validated JSON under strict prompt count limits."""
    validate_prompt_budget(prompt, system)
    return GeminiClient().generate_json(prompt, system=system, temperature=temperature,
                                        max_output_tokens=max_output_tokens, response_schema=response_schema)


def embed(texts: list[str]) -> list[list[float]]:
    """Remote embedding is disabled; use src.local_index for all retrieval."""
    raise GeminiError("Remote embeddings disabled: local hybrid index is required")
